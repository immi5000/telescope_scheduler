"""Telescopes, cameras and the rigs made from them.

The form offers a telescope and a camera separately, because that is how
people own them: one refractor, two cameras, or the other way round. Choosing
a preset fills in every field the calculation reads, and every field stays
editable -- a preset is a starting point, not a constraint.

Every number here is either a manufacturer figure or an explicitly labelled
estimate, and each preset's ``notes`` says which. Two figures are estimates by
nature and scale the signal linearly, so they are called out rather than
buried: optical THROUGHPUT, and the EFFECTIVE V-band quantum efficiency of a
colour sensor, which is roughly half its advertised peak because two of every
four Bayer pixels sit behind red or blue dye.

Complete rigs (``EQUIPMENT``) survive for ``equipmentId``: they are a telescope
preset plus a camera preset plus mount timings, so a rig can never disagree
with the parts it is made of.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tscheduler.api import schemas
from tscheduler.domain.equipment import Camera, Mount, Optics

#: The seeing at which the form quotes star size and sampling. Matches the
#: exposure calculator's own reference condition (physics/quality.py), so the
#: numbers beside the inputs describe the same star the scheduler plans for.
REFERENCE_SEEING_ARCSEC = 2.5

#: Pixels across a star's FWHM. Under ~1.5 stars are blocky and the aperture
#: model is at the edge of validity; over ~3.5 each pixel gets so little
#: signal that read noise starts to matter and 2x2 binning is the usual cure.
UNDERSAMPLED_BELOW = 1.5
OVERSAMPLED_ABOVE = 3.5


@dataclass(frozen=True, slots=True)
class TelescopePreset:
    id: str
    name: str
    manufacturer: str
    design: str
    aperture_mm: float
    focal_length_mm: float
    """Native."""
    central_obstruction_mm: float
    throughput: float
    reducer: float = 1.0
    notes: str = ""

    def optics(self, reducer: float | None = None) -> Optics:
        r = self.reducer if reducer is None else reducer
        return Optics(
            aperture_mm=self.aperture_mm,
            focal_length_mm=self.focal_length_mm * r,
            central_obstruction_mm=self.central_obstruction_mm,
            throughput=self.throughput,
        )


@dataclass(frozen=True, slots=True)
class CameraPreset:
    id: str
    name: str
    sensor: str
    color: bool
    pixel_size_um: float
    sensor_width_px: int
    sensor_height_px: int
    quantum_efficiency: float
    """Effective V-band -- see the module docstring."""
    peak_qe: float
    read_noise_e: float
    dark_current_e_per_s: float
    full_well_e: float
    gain_e_per_adu: float = 1.0
    readout_s: float = 2.0
    notes: str = ""

    def camera(self) -> Camera:
        return Camera(
            pixel_size_um=self.pixel_size_um,
            sensor_width_px=self.sensor_width_px,
            sensor_height_px=self.sensor_height_px,
            quantum_efficiency=self.quantum_efficiency,
            read_noise_e=self.read_noise_e,
            dark_current_e_per_s=self.dark_current_e_per_s,
            full_well_e=self.full_well_e,
            gain_e_per_adu=self.gain_e_per_adu,
            readout_s=self.readout_s,
        )


@dataclass(frozen=True, slots=True)
class EquipmentPreset:
    """A resolved rig: what a session is built from.

    ``optics.focal_length_mm`` is the EFFECTIVE focal length; the native one
    and the reducer are kept beside it so the UI can show both.
    """

    id: str
    name: str
    optics: Optics
    camera: Camera
    mount: Mount
    telescope_id: str | None = None
    telescope_name: str | None = None
    camera_id: str | None = None
    camera_name: str | None = None
    native_focal_length_mm: float | None = None
    reducer: float = 1.0

    @property
    def pixel_scale_arcsec(self) -> float:
        return self.camera.pixel_scale_arcsec(self.optics)

    @property
    def fov_deg(self) -> tuple[float, float]:
        return self.camera.fov_deg(self.optics)


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------

#: Researched from manufacturer pages and checked by a second, independent pass.
#: Reduced configurations are their own rows, because a reducer adds glass and
#: so lowers throughput; picking a reducer in the form on a native row does not.
TELESCOPES: tuple[TelescopePreset, ...] = (
    TelescopePreset(
        "celestron-c8",
        "Celestron C8 SCT f/10",
        "Celestron",
        "sct",
        aperture_mm=203.2,
        focal_length_mm=2032.0,
        central_obstruction_mm=68.0,
        throughput=0.85,
        notes="Celestron: 203.2 mm, 2032 mm, 64 mm secondary mirror. 68 mm is used because the mirror's housing and baffle block about 35% (ScopeViews). Throughput is an estimate from Celestron's StarBright XLT curves. SCT focal length shifts a few percent with focus position.",
    ),
    TelescopePreset(
        "celestron-c8-r063",
        "Celestron C8 SCT + 0.63× reducer f/6.3",
        "Celestron",
        "sct",
        aperture_mm=203.2,
        focal_length_mm=2032.0,
        central_obstruction_mm=68.0,
        throughput=0.81,
        reducer=0.63,
        notes="Celestron's 0.63× reducer/corrector gives 1280 mm at f/6.3. Obstruction as for the C8. Throughput is an estimate (the four-element reducer at about 0.96).",
    ),
    TelescopePreset(
        "celestron-edgehd-8",
        "Celestron EdgeHD 8 f/10",
        "Celestron",
        "aplanatic-sct",
        aperture_mm=203.2,
        focal_length_mm=2032.0,
        central_obstruction_mm=68.6,
        throughput=0.83,
        notes="Celestron: 203.2 mm, 2032 mm, 68.6 mm (2.7″) obstruction. Celestron's white paper measures 2125 mm at the designed back focus. Throughput is an estimate (XLT coatings and two corrector lenses).",
    ),
    TelescopePreset(
        "celestron-edgehd-8-r07",
        "Celestron EdgeHD 8 + 0.7× reducer f/7",
        "Celestron",
        "aplanatic-sct",
        aperture_mm=203.2,
        focal_length_mm=2032.0,
        central_obstruction_mm=68.6,
        throughput=0.79,
        reducer=0.7,
        notes="Celestron's 0.7× EdgeHD reducer gives 1422 mm. Throughput is an estimate.",
    ),
    TelescopePreset(
        "celestron-edgehd-925",
        "Celestron EdgeHD 9.25 f/10",
        "Celestron",
        "aplanatic-sct",
        aperture_mm=234.95,
        focal_length_mm=2350.0,
        central_obstruction_mm=85.0,
        throughput=0.83,
        notes="Celestron: 235 mm, 2350 mm, 85 mm obstruction. Throughput is an estimate.",
    ),
    TelescopePreset(
        "celestron-edgehd-11",
        "Celestron EdgeHD 11 f/10",
        "Celestron",
        "aplanatic-sct",
        aperture_mm=279.4,
        focal_length_mm=2800.0,
        central_obstruction_mm=95.0,
        throughput=0.83,
        notes="Celestron: 279.4 mm, 2800 mm, 95 mm obstruction. Throughput is an estimate.",
    ),
    TelescopePreset(
        "celestron-edgehd-11-r07",
        "Celestron EdgeHD 11 + 0.7× reducer f/7",
        "Celestron",
        "aplanatic-sct",
        aperture_mm=279.4,
        focal_length_mm=2800.0,
        central_obstruction_mm=95.0,
        throughput=0.78,
        reducer=0.7,
        notes="Celestron's 0.7× EdgeHD reducer gives 1960 mm. Throughput is an estimate (five-element reducer).",
    ),
    TelescopePreset(
        "celestron-edgehd-14",
        "Celestron EdgeHD 14 f/11",
        "Celestron",
        "aplanatic-sct",
        aperture_mm=356.0,
        focal_length_mm=3910.0,
        central_obstruction_mm=114.0,
        throughput=0.83,
        notes="Celestron: 356 mm, 3910 mm at f/11, 114 mm obstruction. Throughput is an estimate.",
    ),
    TelescopePreset(
        "celestron-rasa-8",
        "Celestron RASA 8 f/2",
        "Celestron",
        "rasa",
        aperture_mm=203.0,
        focal_length_mm=400.0,
        central_obstruction_mm=93.0,
        throughput=0.84,
        notes="Celestron: 203 mm, 400 mm at f/2, 93 mm (46%) obstruction. 22 mm image circle, so APS-C sensors and smaller. Throughput is an estimate.",
    ),
    TelescopePreset(
        "celestron-rasa-11",
        "Celestron RASA 11 V2 f/2.2",
        "Celestron",
        "rasa",
        aperture_mm=279.0,
        focal_length_mm=620.0,
        central_obstruction_mm=114.0,
        throughput=0.84,
        notes="Celestron: 279 mm, 620 mm at f/2.2, 114 mm (41%) obstruction, 43 mm image circle. Throughput is an estimate.",
    ),
    TelescopePreset(
        "astro-tech-at8rc",
        'Astro-Tech AT8RC 8" Ritchey-Chrétien f/8',
        "Astro-Tech",
        "ritchey-chretien",
        aperture_mm=203.0,
        focal_length_mm=1625.0,
        central_obstruction_mm=95.0,
        throughput=0.86,
        notes="Astro-Tech: 203 mm, 1625 mm at f/8, 95 mm (47%) secondary-holder obstruction. Throughput is an estimate; vendor reflectivity claims of 94–99% disagree.",
    ),
    TelescopePreset(
        "skywatcher-quattro-200p",
        "Sky-Watcher Quattro 200P f/4",
        "Sky-Watcher",
        "newtonian",
        aperture_mm=205.0,
        focal_length_mm=800.0,
        central_obstruction_mm=70.0,
        throughput=0.82,
        notes="Sky-Watcher: 205 mm, 800 mm at f/3.9, 70 mm (34%) obstruction. It needs a coma corrector, which the throughput estimate includes.",
    ),
    TelescopePreset(
        "skywatcher-explorer-200pds",
        "Sky-Watcher Explorer 200PDS f/5",
        "Sky-Watcher",
        "newtonian",
        aperture_mm=200.0,
        focal_length_mm=1000.0,
        central_obstruction_mm=58.0,
        throughput=0.82,
        notes="Sky-Watcher distributor figures: 200 mm, 1000 mm at f/5, 58 mm secondary. The throughput estimate includes a coma corrector.",
    ),
    TelescopePreset(
        "skywatcher-explorer-200p",
        "Sky-Watcher Explorer 200P f/5",
        "Sky-Watcher",
        "newtonian",
        aperture_mm=200.0,
        focal_length_mm=1000.0,
        central_obstruction_mm=52.0,
        throughput=0.82,
        notes="200 mm, 1000 mm at f/5. The 52 mm secondary is a retailer figure (±3 mm). Throughput is an estimate. The 200PDS is the imaging version.",
    ),
    TelescopePreset(
        "william-optics-redcat-51",
        "William Optics RedCat 51 f/4.9",
        "William Optics",
        "refractor",
        aperture_mm=51.0,
        focal_length_mm=250.0,
        central_obstruction_mm=0.0,
        throughput=0.95,
        notes="William Optics: 51 mm, 250 mm at f/4.9, four-element Petzval. Throughput is an estimate.",
    ),
    TelescopePreset(
        "william-optics-zenithstar-73-iii",
        "William Optics Zenithstar 73 III f/5.9",
        "William Optics",
        "refractor",
        aperture_mm=73.0,
        focal_length_mm=430.0,
        central_obstruction_mm=0.0,
        throughput=0.94,
        notes="William Optics: 73 mm, 430 mm at f/5.9, FPL-53 doublet (discontinued). The throughput estimate includes the FLAT 73A flattener.",
    ),
    TelescopePreset(
        "william-optics-zenithstar-73-iii-flat73r-0p8x",
        "William Optics Zenithstar 73 III + 0.8× Flat73R f/4.7",
        "William Optics",
        "refractor",
        aperture_mm=73.0,
        focal_length_mm=430.0,
        central_obstruction_mm=0.0,
        throughput=0.92,
        reducer=0.8,
        notes="The FLAT 73R 0.8× reducer gives 344 mm (0.8 × 430). Throughput is an estimate.",
    ),
    TelescopePreset(
        "william-optics-gt81-iv",
        "William Optics Gran Turismo GT81 IV f/5.9",
        "William Optics",
        "refractor",
        aperture_mm=81.0,
        focal_length_mm=478.0,
        central_obstruction_mm=0.0,
        throughput=0.94,
        notes="William Optics: 81 mm, 478 mm at f/5.9, triplet. The throughput estimate includes the FLAT GT flattener.",
    ),
    TelescopePreset(
        "william-optics-gt81-iv-flat6aiii-0p8x",
        "William Optics GT81 IV + 0.8× Flat6A III f/4.7",
        "William Optics",
        "refractor",
        aperture_mm=81.0,
        focal_length_mm=478.0,
        central_obstruction_mm=0.0,
        throughput=0.91,
        reducer=0.8,
        notes="The Flat6A III 0.8× reducer gives 382 mm (0.8 × 478). Throughput is an estimate.",
    ),
    TelescopePreset(
        "skywatcher-evostar-72ed",
        "Sky-Watcher Evostar 72ED f/5.8",
        "Sky-Watcher",
        "refractor",
        aperture_mm=72.0,
        focal_length_mm=420.0,
        central_obstruction_mm=0.0,
        throughput=0.96,
        notes="Sky-Watcher: 72 mm, 420 mm at f/5.8, ED doublet. The throughput estimate is for the bare tube.",
    ),
    TelescopePreset(
        "skywatcher-evostar-72ed-0p85x",
        "Sky-Watcher Evostar 72ED + 0.85× reducer f/4.9",
        "Sky-Watcher",
        "refractor",
        aperture_mm=72.0,
        focal_length_mm=420.0,
        central_obstruction_mm=0.0,
        throughput=0.94,
        reducer=0.8452,
        notes="The 0.85× reducer/flattener gives 355 mm (Sky-Watcher). Throughput is an estimate.",
    ),
    TelescopePreset(
        "skywatcher-evolux-62ed",
        "Sky-Watcher Evolux 62ED f/6.45",
        "Sky-Watcher",
        "refractor",
        aperture_mm=62.0,
        focal_length_mm=400.0,
        central_obstruction_mm=0.0,
        throughput=0.96,
        notes="Sky-Watcher: 62 mm, 400 mm at f/6.45, ED doublet. The throughput estimate is for the bare tube.",
    ),
    TelescopePreset(
        "skywatcher-evolux-62ed-0p9x",
        "Sky-Watcher Evolux 62ED + 0.9× reducer f/5.8",
        "Sky-Watcher",
        "refractor",
        aperture_mm=62.0,
        focal_length_mm=400.0,
        central_obstruction_mm=0.0,
        throughput=0.94,
        reducer=0.9,
        notes="The 0.9× reducer gives 360 mm (Sky-Watcher). Throughput is an estimate.",
    ),
    TelescopePreset(
        "skywatcher-evostar-80ed",
        "Sky-Watcher Evostar 80ED f/7.5",
        "Sky-Watcher",
        "refractor",
        aperture_mm=80.0,
        focal_length_mm=600.0,
        central_obstruction_mm=0.0,
        throughput=0.96,
        notes="Sky-Watcher: 80 mm, 600 mm at f/7.5, ED doublet; also covers the 80EDX. The throughput estimate is for the bare tube.",
    ),
    TelescopePreset(
        "skywatcher-evostar-80ed-0p85x",
        "Sky-Watcher Evostar 80ED + 0.85× reducer f/6.4",
        "Sky-Watcher",
        "refractor",
        aperture_mm=80.0,
        focal_length_mm=600.0,
        central_obstruction_mm=0.0,
        throughput=0.94,
        reducer=0.85,
        notes="The 0.85× reducer/corrector gives 510 mm (0.85 × 600). Throughput is an estimate.",
    ),
    TelescopePreset(
        "skywatcher-esprit-100ed",
        "Sky-Watcher Esprit 100ED f/5.5",
        "Sky-Watcher",
        "refractor",
        aperture_mm=100.0,
        focal_length_mm=550.0,
        central_obstruction_mm=0.0,
        throughput=0.92,
        notes="Sky-Watcher: 100 mm, 550 mm at f/5.5, triplet with its flattener; also covers the 100EDX. Throughput is an estimate.",
    ),
    TelescopePreset(
        "askar-fra400",
        "Askar FRA400 f/5.6",
        "Askar",
        "refractor",
        aperture_mm=72.0,
        focal_length_mm=400.0,
        central_obstruction_mm=0.0,
        throughput=0.92,
        notes="Askar/Sharpstar: 72 mm, 400 mm at f/5.6, quintuplet with a built-in flattener. Throughput is an estimate.",
    ),
    TelescopePreset(
        "askar-fra400-0p7x",
        "Askar FRA400 + 0.7× reducer f/3.9",
        "Askar",
        "refractor",
        aperture_mm=72.0,
        focal_length_mm=400.0,
        central_obstruction_mm=0.0,
        throughput=0.88,
        reducer=0.7,
        notes="The 0.7× full-frame reducer gives 280 mm (Sharpstar). Throughput is an estimate.",
    ),
    TelescopePreset(
        "takahashi-fsq-85edp",
        "Takahashi FSQ-85EDP f/5.3",
        "Takahashi",
        "refractor",
        aperture_mm=85.0,
        focal_length_mm=450.0,
        central_obstruction_mm=0.0,
        throughput=0.94,
        notes="Takahashi: 85 mm, 450 mm at f/5.3, Petzval; sold as the FSQ-85EDX outside Japan. Throughput is an estimate.",
    ),
    TelescopePreset(
        "takahashi-fsq-85edp-qb-0p73x",
        "Takahashi FSQ-85EDP + 0.73× reducer QB f/3.9",
        "Takahashi",
        "refractor",
        aperture_mm=85.0,
        focal_length_mm=450.0,
        central_obstruction_mm=0.0,
        throughput=0.91,
        reducer=0.7333,
        notes="The QB 0.73× reducer gives 330 mm (Takahashi). Throughput is an estimate.",
    ),
    TelescopePreset(
        "takahashi-fsq-106edx4",
        "Takahashi FSQ-106EDX4 f/5",
        "Takahashi",
        "refractor",
        aperture_mm=106.0,
        focal_length_mm=530.0,
        central_obstruction_mm=0.0,
        throughput=0.94,
        notes="Takahashi: 106 mm, 530 mm at f/5, Petzval with an 88 mm image circle. Throughput is an estimate.",
    ),
    TelescopePreset(
        "takahashi-fsq-106edx4-qe-0p73x",
        "Takahashi FSQ-106EDX4 + 0.73× reducer QE f/3.6",
        "Takahashi",
        "refractor",
        aperture_mm=106.0,
        focal_length_mm=530.0,
        central_obstruction_mm=0.0,
        throughput=0.89,
        reducer=0.7264,
        notes="The RD QE 0.73× reducer gives 385 mm (Takahashi). Throughput is an estimate.",
    ),
    TelescopePreset(
        "explore-scientific-ed102-fcd100",
        "Explore Scientific ED102 FCD100 f/7",
        "Explore Scientific",
        "refractor",
        aperture_mm=102.0,
        focal_length_mm=714.0,
        central_obstruction_mm=0.0,
        throughput=0.92,
        notes="Explore Scientific: 102 mm, 714 mm at f/7, FCD100 triplet. The throughput estimate includes a flattener.",
    ),
    TelescopePreset(
        "stellarvue-svx80t-3sv",
        "Stellarvue SVX80T-3SV f/6",
        "Stellarvue",
        "refractor",
        aperture_mm=80.0,
        focal_length_mm=480.0,
        central_obstruction_mm=0.0,
        throughput=0.92,
        notes="Stellarvue: 80 mm, 480 mm at f/6, triplet with the SFF flattener (discontinued). Throughput is an estimate.",
    ),
    TelescopePreset(
        "stellarvue-svx80t-3sv-sffr-0p74x",
        "Stellarvue SVX80T-3SV + 0.74× SFFR f/4.5",
        "Stellarvue",
        "refractor",
        aperture_mm=80.0,
        focal_length_mm=480.0,
        central_obstruction_mm=0.0,
        throughput=0.9,
        reducer=0.7417,
        notes="The SFFR 0.74× reducer gives 356 mm (Stellarvue). Throughput is an estimate.",
    ),
)

#: Researched from manufacturer manuals and graphs, then checked by a second,
#: independent pass. Read noise, gain, dark current and full well are all at
#: the gain people image at -- full well at gain 0 is the headline figure and
#: four times what the ADC can hold at unity gain. Gain is in NATIVE ADC units.
CAMERAS: tuple[CameraPreset, ...] = (
    CameraPreset(
        "zwo-asi2600mm-pro",
        "ZWO ASI2600MM Pro",
        "Sony IMX571 (mono)",
        color=False,
        pixel_size_um=3.76,
        sensor_width_px=6248,
        sensor_height_px=4176,
        quantum_efficiency=0.83,
        peak_qe=0.91,
        read_noise_e=1.4,
        dark_current_e_per_s=0.0007,
        full_well_e=16400.0,
        gain_e_per_adu=0.25,
        readout_s=0.164,
        notes="ZWO figures at gain 100 (HCG): 1.4 e⁻ read noise, 0.0007 e⁻/s dark at -10 °C. Effective QE is ZWO's QE curve weighted by the V band (peak 91%).",
    ),
    CameraPreset(
        "zwo-asi2600mc-pro",
        "ZWO ASI2600MC Pro",
        "Sony IMX571 (colour)",
        color=True,
        pixel_size_um=3.76,
        sensor_width_px=6248,
        sensor_height_px=4176,
        quantum_efficiency=0.42,
        peak_qe=0.8,
        read_noise_e=1.4,
        dark_current_e_per_s=0.0006,
        full_well_e=16400.0,
        gain_e_per_adu=0.25,
        readout_s=0.285,
        notes="ZWO figures at gain 100 (HCG). Effective QE is an estimate: ZWO's R/G/B curves averaged over the Bayer cell and weighted by the V band (green pixels alone reach 0.68).",
    ),
    CameraPreset(
        "zwo-asi533mc-pro",
        "ZWO ASI533MC Pro",
        "Sony IMX533 (colour)",
        color=True,
        pixel_size_um=3.76,
        sensor_width_px=3008,
        sensor_height_px=3008,
        quantum_efficiency=0.42,
        peak_qe=0.8,
        read_noise_e=1.5,
        dark_current_e_per_s=0.0005,
        full_well_e=16000.0,
        gain_e_per_adu=1,
        readout_s=0.05,
        notes="ZWO figures at gain 100 (unity, HCG): 1.5 e⁻, 0.0005 e⁻/s at -10 °C. Effective QE is a V-band estimate averaged over the Bayer cell.",
    ),
    CameraPreset(
        "zwo-asi533mm-pro",
        "ZWO ASI533MM Pro",
        "Sony IMX533 (mono)",
        color=False,
        pixel_size_um=3.76,
        sensor_width_px=3008,
        sensor_height_px=3008,
        quantum_efficiency=0.83,
        peak_qe=0.91,
        read_noise_e=1.5,
        dark_current_e_per_s=0.0006,
        full_well_e=16000.0,
        gain_e_per_adu=1,
        readout_s=0.05,
        notes="ZWO figures at gain 100 (unity, HCG): 1.5 e⁻, 0.0006 e⁻/s at -10 °C. Effective QE is ZWO's QE curve weighted by the V band.",
    ),
    CameraPreset(
        "zwo-asi6200mm-pro",
        "ZWO ASI6200MM Pro",
        "Sony IMX455 (mono)",
        color=False,
        pixel_size_um=3.76,
        sensor_width_px=9576,
        sensor_height_px=6388,
        quantum_efficiency=0.83,
        peak_qe=0.91,
        read_noise_e=1.4,
        dark_current_e_per_s=0.0005,
        full_well_e=16700.0,
        gain_e_per_adu=0.25,
        readout_s=0.488,
        notes="ZWO figures at gain 100 (HCG): 1.4 e⁻, 0.0005 e⁻/s at -10 °C, full-frame sensor. Effective QE is ZWO's QE curve weighted by the V band.",
    ),
    CameraPreset(
        "zwo-asi294mc-pro",
        "ZWO ASI294MC Pro",
        "Sony IMX294 (colour)",
        color=True,
        pixel_size_um=4.63,
        sensor_width_px=4144,
        sensor_height_px=2822,
        quantum_efficiency=0.39,
        peak_qe=0.75,
        read_noise_e=1.8,
        dark_current_e_per_s=0.0087,
        full_well_e=14200.0,
        gain_e_per_adu=0.9,
        readout_s=0.0625,
        notes="ZWO figures at gain 120 (HCG): 1.8 e⁻, 0.0087 e⁻/s at -10 °C; warmer than the IMX571 and prone to amp glow. Effective QE is a V-band estimate averaged over the Bayer cell.",
    ),
    CameraPreset(
        "zwo-asi294mm-pro",
        "ZWO ASI294MM Pro",
        "Sony IMX492 (mono, Bin2)",
        color=False,
        pixel_size_um=4.63,
        sensor_width_px=4144,
        sensor_height_px=2822,
        quantum_efficiency=0.8,
        peak_qe=0.84,
        read_noise_e=1.8,
        dark_current_e_per_s=0.0064,
        full_well_e=14600.0,
        gain_e_per_adu=0.9,
        readout_s=0.061,
        notes="Default Bin2 mode. ZWO figures at gain 120 (HCG): 1.8 e⁻, 0.0064 e⁻/s at -10 °C. Effective QE is an estimate: ZWO's curve rescaled to the independently measured ~84% peak (Buil).",
    ),
    CameraPreset(
        "zwo-asi183mm-pro",
        "ZWO ASI183MM Pro",
        "Sony IMX183 (mono)",
        color=False,
        pixel_size_um=2.4,
        sensor_width_px=5496,
        sensor_height_px=3672,
        quantum_efficiency=0.75,
        peak_qe=0.8,
        read_noise_e=2.1,
        dark_current_e_per_s=0.0037,
        full_well_e=3900.0,
        gain_e_per_adu=1,
        readout_s=0.105,
        notes="ZWO figures at unity gain 120 (no HCG): 2.1 e⁻, 0.0037 e⁻/s at -10 °C. Effective QE is an estimate between ZWO's curve and Buil's measurement. Download time is an estimate; ZWO publishes no 16-bit rate.",
    ),
    CameraPreset(
        "zwo-asi1600mm-pro",
        "ZWO ASI1600MM Pro",
        "Panasonic MN34230ALJ (mono)",
        color=False,
        pixel_size_um=3.8,
        sensor_width_px=4656,
        sensor_height_px=3520,
        quantum_efficiency=0.55,
        peak_qe=0.6,
        read_noise_e=1.7,
        dark_current_e_per_s=0.0115,
        full_well_e=4000.0,
        gain_e_per_adu=1,
        readout_s=0.087,
        notes="Discontinued. ZWO manual figures at unity gain 139: 1.7 e⁻, 0.0115 e⁻/s at -10 °C. Front-illuminated; effective QE from ZWO's curve. Download time is an estimate.",
    ),
    CameraPreset(
        "zwo-asi585mc-pro",
        "ZWO ASI585MC Pro",
        "Sony IMX585 (colour)",
        color=True,
        pixel_size_um=2.9,
        sensor_width_px=3840,
        sensor_height_px=2160,
        quantum_efficiency=0.46,
        peak_qe=0.91,
        read_noise_e=1.1,
        dark_current_e_per_s=0.0018,
        full_well_e=3900.0,
        gain_e_per_adu=1,
        readout_s=0.042,
        notes="ZWO figures at gain 200 (HCG): 1.1 e⁻, 0.0018 e⁻/s at -10 °C. A small sensor. Effective QE is a V-band estimate from ZWO's absolute R/G/B curves.",
    ),
    CameraPreset(
        "zwo-asi071mc-pro",
        "ZWO ASI071MC Pro",
        "Sony IMX071 (colour)",
        color=True,
        pixel_size_um=4.78,
        sensor_width_px=4944,
        sensor_height_px=3284,
        quantum_efficiency=0.22,
        peak_qe=0.5,
        read_noise_e=2.6,
        dark_current_e_per_s=0.0012,
        full_well_e=16000.0,
        gain_e_per_adu=1,
        readout_s=0.1,
        notes="Discontinued. ZWO manual figures at unity gain 94: 2.6 e⁻, 0.0012 e⁻/s at -10 °C. Front-illuminated; effective QE is an estimate (peak about 50%).",
    ),
    CameraPreset(
        "qhy268m",
        "QHYCCD QHY268M",
        "Sony IMX571 (mono)",
        color=False,
        pixel_size_um=3.76,
        sensor_width_px=6252,
        sensor_height_px=4176,
        quantum_efficiency=0.82,
        peak_qe=0.92,
        read_noise_e=1.6,
        dark_current_e_per_s=0.001,
        full_well_e=28528.0,
        gain_e_per_adu=0.44,
        readout_s=0.167,
        notes="QHY figures in Mode #5 (High Gain 2CMS) at gain 56: 1.6 e⁻, 0.001 e⁻/s at -10 °C. Effective QE is QHY's QE curve weighted by the V band.",
    ),
    CameraPreset(
        "canon-eos-6d",
        "Canon EOS 6D (unmodified)",
        "Canon full-frame CMOS (colour)",
        color=True,
        pixel_size_um=6.54,
        sensor_width_px=5472,
        sensor_height_px=3648,
        quantum_efficiency=0.23,
        peak_qe=0.5,
        read_noise_e=2.45,
        dark_current_e_per_s=0.23,
        full_well_e=4600.0,
        gain_e_per_adu=0.31,
        readout_s=1.5,
        notes="Uncooled DSLR at ISO 1600: 2.45 e⁻ read noise (PhotonsToPhotos); dark current is 0.23 e⁻/s at about 5 °C (Clarkvision) and climbs steeply when warm. Canon publishes no QE, so it is an estimate. Download time is an estimate for tethered USB 2.0.",
    ),
)

DEFAULT_MOUNT = Mount(switch_minutes=5.0, min_block_minutes=20.0)
DEFAULT_TELESCOPE_ID = "celestron-c8"
DEFAULT_CAMERA_ID = "zwo-asi2600mm-pro"


def telescope_by_id(tid: str | None) -> TelescopePreset | None:
    return next((t for t in TELESCOPES if t.id == tid), None)


def camera_by_id(cid: str | None) -> CameraPreset | None:
    return next((c for c in CAMERAS if c.id == cid), None)


@dataclass(frozen=True, slots=True)
class _Rig:
    id: str
    name: str
    telescope_id: str
    camera_id: str
    mount: Mount = field(default_factory=lambda: DEFAULT_MOUNT)


_RIGS: tuple[_Rig, ...] = (
    _Rig("sct8-2600mm", '8" SCT f/10 + ASI2600MM', "celestron-c8", "zwo-asi2600mm-pro"),
    _Rig(
        "refractor80-533",
        "80mm apo f/6 + ASI533MC",
        "stellarvue-svx80t-3sv",
        "zwo-asi533mc-pro",
        Mount(switch_minutes=3.0, min_block_minutes=15.0),
    ),
    _Rig(
        "edge11-6200",
        'Celestron EdgeHD 11" + ASI6200MM',
        "celestron-edgehd-11",
        "zwo-asi6200mm-pro",
    ),
)


def _assemble(rig: _Rig) -> EquipmentPreset:
    tel = telescope_by_id(rig.telescope_id)
    cam = camera_by_id(rig.camera_id)
    if tel is None or cam is None:  # a data error, caught at import by the tuple below
        raise LookupError(f"rig {rig.id!r} names a missing telescope or camera preset")
    return EquipmentPreset(
        id=rig.id,
        name=rig.name,
        optics=tel.optics(),
        camera=cam.camera(),
        mount=rig.mount,
        telescope_id=tel.id,
        telescope_name=tel.name,
        camera_id=cam.id,
        camera_name=cam.name,
        native_focal_length_mm=tel.focal_length_mm,
        reducer=tel.reducer,
    )


#: Complete rigs, addressable by ``equipmentId``.
EQUIPMENT: tuple[EquipmentPreset, ...] = tuple(_assemble(r) for r in _RIGS)


def equipment_by_id(eq_id: str) -> EquipmentPreset | None:
    return next((e for e in EQUIPMENT if e.id == eq_id), None)


# --------------------------------------------------------------------------
# requests -> domain
# --------------------------------------------------------------------------


def from_request(req: schemas.EquipmentRequest) -> EquipmentPreset:
    """A rig exactly as the form describes it. Validation already happened in
    the request model; nothing here second-guesses a number."""
    o, c, m = req.optics, req.camera, req.mount
    tel = telescope_by_id(req.telescope_id)
    cam = camera_by_id(req.camera_id)
    optics = Optics(
        aperture_mm=o.aperture_mm,
        focal_length_mm=o.focal_length_mm * o.reducer,
        central_obstruction_mm=o.central_obstruction_mm,
        throughput=o.throughput,
    )
    camera = Camera(
        pixel_size_um=c.pixel_size_um,
        sensor_width_px=c.sensor_width_px,
        sensor_height_px=c.sensor_height_px,
        quantum_efficiency=c.quantum_efficiency,
        read_noise_e=c.read_noise_e,
        dark_current_e_per_s=c.dark_current_e_per_s,
        readout_s=c.readout_s,
        # Not read by the calculation, but kept honest when a preset is known.
        full_well_e=cam.full_well_e if cam else 50_000.0,
        gain_e_per_adu=cam.gain_e_per_adu if cam else 1.0,
    )
    # A part that no longer matches its preset arrives without an id; the
    # form's label for it is what the observer recognises.
    tel_name = tel.name if tel else req.telescope_name
    cam_name = cam.name if cam else req.camera_name
    name = req.name or " + ".join(x for x in (tel_name, cam_name) if x)
    return EquipmentPreset(
        id="custom",
        name=name or "Custom rig",
        optics=optics,
        camera=camera,
        mount=Mount(switch_minutes=m.switch_minutes, min_block_minutes=m.min_block_minutes),
        telescope_id=tel.id if tel else None,
        telescope_name=tel_name,
        camera_id=cam.id if cam else None,
        camera_name=cam_name,
        native_focal_length_mm=o.focal_length_mm,
        reducer=o.reducer,
    )


def resolve(equipment_id: str, equipment: schemas.EquipmentRequest | None) -> EquipmentPreset:
    """The rig a request means. A full description wins over an id.

    Raises ``LookupError`` for an unknown id; the HTTP layer turns that into a
    422 naming the ids that do exist.
    """
    if equipment is not None:
        return from_request(equipment)
    found = equipment_by_id(equipment_id)
    if found is None:
        raise LookupError(f"unknown equipmentId {equipment_id!r}; try {[e.id for e in EQUIPMENT]}")
    return found


# --------------------------------------------------------------------------
# domain -> wire
# --------------------------------------------------------------------------


def _r(x: float, nd: int) -> float:
    return round(float(x), nd)


def sampling_label(px_per_fwhm: float) -> str:
    if px_per_fwhm < UNDERSAMPLED_BELOW:
        return "undersampled"
    if px_per_fwhm > OVERSAMPLED_ABOVE:
        return "oversampled"
    return "well-sampled"


def equipment_out(p: EquipmentPreset) -> schemas.EquipmentOut:
    o, c, m = p.optics, p.camera, p.mount
    w, h = p.fov_deg
    scale = p.pixel_scale_arcsec
    psf = o.psf_fwhm_arcsec(REFERENCE_SEEING_ARCSEC)
    per_fwhm = psf / scale
    native = p.native_focal_length_mm or o.focal_length_mm / (p.reducer or 1.0)
    return schemas.EquipmentOut(
        id=p.id,
        name=p.name,
        telescope_id=p.telescope_id,
        telescope_name=p.telescope_name,
        camera_id=p.camera_id,
        camera_name=p.camera_name,
        aperture_mm=o.aperture_mm,
        native_focal_length_mm=_r(native, 1),
        reducer=p.reducer,
        focal_length_mm=_r(o.focal_length_mm, 1),
        focal_ratio=_r(o.focal_ratio, 2),
        central_obstruction_mm=o.central_obstruction_mm,
        obstruction_fraction=_r(o.central_obstruction_mm / o.aperture_mm, 3),
        collecting_area_cm2=_r(o.collecting_area_cm2, 1),
        throughput=o.throughput,
        dawes_limit_arcsec=_r(o.dawes_limit_arcsec, 2),
        rayleigh_limit_arcsec=_r(o.rayleigh_limit_arcsec, 2),
        diffraction_fwhm_arcsec=_r(o.diffraction_fwhm_arcsec, 2),
        reference_seeing_arcsec=REFERENCE_SEEING_ARCSEC,
        psf_fwhm_arcsec=_r(psf, 2),
        sampling_px_per_fwhm=_r(per_fwhm, 2),
        sampling=sampling_label(per_fwhm),
        pixel_size_um=c.pixel_size_um,
        sensor_width_px=c.sensor_width_px,
        sensor_height_px=c.sensor_height_px,
        pixel_scale_arcsec=_r(scale, 3),
        fov_width_deg=_r(w, 4),
        fov_height_deg=_r(h, 4),
        quantum_efficiency=c.quantum_efficiency,
        read_noise_e=c.read_noise_e,
        dark_current_e_per_s=c.dark_current_e_per_s,
        readout_s=c.readout_s,
        switch_minutes=m.switch_minutes,
        min_block_minutes=m.min_block_minutes,
    )


def telescope_out(t: TelescopePreset) -> schemas.TelescopePresetOut:
    o = t.optics()
    return schemas.TelescopePresetOut(
        id=t.id,
        name=t.name,
        manufacturer=t.manufacturer,
        design=t.design,
        aperture_mm=t.aperture_mm,
        focal_length_mm=t.focal_length_mm,
        reducer=t.reducer,
        focal_ratio=_r(o.focal_ratio, 2),
        central_obstruction_mm=t.central_obstruction_mm,
        throughput=t.throughput,
        dawes_limit_arcsec=_r(o.dawes_limit_arcsec, 2),
        notes=t.notes,
    )


def camera_out(c: CameraPreset) -> schemas.CameraPresetOut:
    return schemas.CameraPresetOut(
        id=c.id,
        name=c.name,
        sensor=c.sensor,
        color=c.color,
        pixel_size_um=c.pixel_size_um,
        sensor_width_px=c.sensor_width_px,
        sensor_height_px=c.sensor_height_px,
        quantum_efficiency=c.quantum_efficiency,
        peak_qe=c.peak_qe,
        read_noise_e=c.read_noise_e,
        dark_current_e_per_s=c.dark_current_e_per_s,
        readout_s=c.readout_s,
        notes=c.notes,
    )


def mount_out(m: Mount) -> schemas.MountOut:
    return schemas.MountOut(switch_minutes=m.switch_minutes, min_block_minutes=m.min_block_minutes)
