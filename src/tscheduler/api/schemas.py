"""Wire types.

Two rules hold throughout, and both are load-bearing rather than stylistic.

**Every displayed number is computed here, not in the browser.** The moment the
client derives an altitude or a field-of-view corner it will disagree in the
third decimal, and someone will spend an evening on a card reading 30.1 while
the timeline draws 29.9 and the planner rejected the block for being under 30.
So the payload carries sexagesimal strings, compass words and slot timestamps,
even though all three are trivially derivable.

**Every plan states the window it is valid for.** ``validFrom``/``validUntil``
are what collapse a scrub from one request per pointer-move to one request per
decision point -- between two consecutive publication instants the plan is
identical by construction, so the client can cache on that interval.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class Api(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------


class SiteRequest(Api):
    latitude_deg: float = Field(ge=-90, le=90)
    longitude_deg: float = Field(ge=-180, le=360, description="positive EAST")
    elevation_m: float = 0.0
    name: str = "custom"
    bortle: int = Field(default=5, ge=1, le=9)
    extinction_k: float = Field(default=0.20, gt=0, lt=2)
    min_altitude_deg: float = Field(default=30.0, ge=0, lt=90)
    min_moon_separation_deg: float = Field(default=15.0, ge=0, le=180)

    @field_validator("longitude_deg")
    @classmethod
    def _wrap_longitude(cls, v: float) -> float:
        # 271.76 E is 88.24 W; everything downstream (the solar offset that
        # picks the local night, above all) assumes -180..180.
        return ((v + 180.0) % 360.0) - 180.0


class TargetRequest(Api):
    id: str
    name: str | None = None
    ra_deg: float | None = Field(default=None, ge=0, lt=360)
    dec_deg: float | None = Field(default=None, ge=-90, le=90)
    magnitude: float | None = Field(default=None, description="V surface brightness, mag/arcsec^2")
    priority: float = Field(default=1.0, gt=0)
    urgency: float = Field(default=1.0, gt=0)
    snr_goal: float | None = Field(default=None, gt=0)


class OpticsRequest(Api):
    """The telescope, as the exposure calculator sees it.

    Focal ratio is not a field: it is ``focal_length / aperture`` and accepting
    it as well would allow a request that contradicts itself. The form offers
    it as an input, and turns it into a focal length before it gets here.
    """

    aperture_mm: float = Field(gt=10, le=2000, description="clear aperture")
    focal_length_mm: float = Field(
        gt=20, le=30_000, description="NATIVE focal length, before any reducer or Barlow"
    )
    reducer: float = Field(
        default=1.0,
        ge=0.2,
        le=5.0,
        description="focal-length multiplier: 0.63 for a reducer, 2.0 for a Barlow, 1 for none",
    )
    central_obstruction_mm: float = Field(
        default=0.0, ge=0, description="DIAMETER of the secondary obstruction; 0 for a refractor"
    )
    throughput: float = Field(
        default=0.80, gt=0, le=1, description="total optical transmission in V, an estimate"
    )

    @model_validator(mode="after")
    def _obstruction_inside_aperture(self) -> OpticsRequest:
        if self.central_obstruction_mm >= self.aperture_mm:
            raise ValueError(
                f"central obstruction {self.central_obstruction_mm} mm must be smaller than "
                f"the aperture {self.aperture_mm} mm"
            )
        if self.focal_length_mm * self.reducer <= 20:
            raise ValueError(
                f"effective focal length {self.focal_length_mm * self.reducer:.1f} mm "
                f"(native x reducer) must be over 20 mm"
            )
        return self


class CameraRequest(Api):
    """The sensor. Only fields the calculation actually reads are accepted."""

    pixel_size_um: float = Field(gt=0.5, le=30)
    sensor_width_px: int = Field(ge=64, le=50_000)
    sensor_height_px: int = Field(ge=64, le=50_000)
    quantum_efficiency: float = Field(
        gt=0,
        le=1,
        description="EFFECTIVE V-band QE; roughly half the mono figure for a colour sensor",
    )
    read_noise_e: float = Field(ge=0, le=50, description="e- RMS at the gain you image at")
    dark_current_e_per_s: float = Field(ge=0, le=5, description="e-/s/pixel at your set-point")
    readout_s: float = Field(default=2.0, ge=0, le=120, description="full-frame download time")


class MountRequest(Api):
    switch_minutes: float = Field(
        default=5.0, ge=0, le=60, description="slew, centre and refocus on a new target"
    )
    min_block_minutes: float = Field(default=20.0, ge=1, le=240)


class EquipmentRequest(Api):
    """A full rig. Overrides ``equipmentId`` wherever both are sent."""

    name: str | None = None
    telescope_id: str | None = Field(
        default=None, description="the telescope preset this started from, for display only"
    )
    camera_id: str | None = Field(
        default=None, description="the camera preset this started from, for display only"
    )
    telescope_name: str | None = Field(
        default=None,
        description="display label for the telescope when it no longer matches a preset",
    )
    camera_name: str | None = Field(
        default=None, description="display label for the camera when it no longer matches a preset"
    )
    optics: OpticsRequest
    camera: CameraRequest
    mount: MountRequest = Field(default_factory=MountRequest)


class SessionRequest(Api):
    name: str | None = None
    date: str = Field(description="night start date, UTC, YYYY-MM-DD")
    start_hour_utc: float = Field(default=1.0, ge=0, lt=24)
    hours: float = Field(default=8.0, gt=0, le=16)
    slot_minutes: int = Field(default=5, ge=1, le=30)

    site_id: str | None = "urbana"
    site: SiteRequest | None = Field(default=None, description="overrides siteId when present")
    equipment_id: str = "sct8-2600mm"
    equipment: EquipmentRequest | None = Field(
        default=None, description="overrides equipmentId when present"
    )

    snr_goal: float = Field(
        default=15.0,
        gt=0,
        description="per star-sized patch of the target's surface, at its MEAN brightness",
    )
    t_sub_s: float = Field(default=90.0, gt=0)
    targets: list[TargetRequest] | None = None

    weather: str = Field(
        default="auto",
        description=(
            "'auto' (real data, chosen by when the night is: archived model runs for a "
            "past night, the live forecast for tonight or a coming night), 'synthetic' "
            "(offline and deterministic -- tests and demos only), or 'open_meteo' "
            "(archived runs only)"
        ),
    )
    solve_seconds: float = Field(default=4.0, gt=0, le=60)


class TonightRequest(Api):
    """What is worth imaging from a site on a night, with a given rig."""

    date: str = Field(description="night start date, YYYY-MM-DD, local to the site")
    site_id: str | None = "urbana"
    site: SiteRequest | None = Field(default=None, description="overrides siteId when present")
    equipment_id: str = "sct8-2600mm"
    equipment: EquipmentRequest | None = Field(
        default=None, description="overrides equipmentId when present"
    )
    snr_goal: float = Field(default=15.0, gt=0)
    t_sub_s: float = Field(default=90.0, gt=0)
    start: datetime | None = Field(
        default=None, description="window start; omitted means astronomical dusk"
    )
    hours: float | None = Field(
        default=None, gt=0, le=16, description="window length; omitted means dusk to dawn"
    )


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------


class LocationOut(Api):
    """A coordinate, decorated. ``GET /api/site/locate``.

    The two nullable fields are looked up from services that may be down, and
    neither is needed for the site to be planned: the latitude and longitude
    echoed back are the answer, and they are the ones that were asked about.
    A null ``name`` means "we could not find out", never "nowhere".
    """

    latitude_deg: float
    longitude_deg: float
    name: str | None = None
    """The town or city, and its state or country: "Champaign, Illinois"."""
    elevation_m: float | None = None
    """Ground elevation from a terrain model, NOT the browser's GPS altitude."""
    attribution: str


class SitePresetOut(Api):
    id: str
    name: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float
    bortle: int
    extinction_k: float
    artificial_zenith_nl: float
    zenith_sky_mag_arcsec2: float
    min_altitude_deg: float
    min_moon_separation_deg: float


class EquipmentOut(Api):
    """A rig, with every number a panel displays about it.

    Served for the preset rigs, for a session's equipment, and by
    ``POST /api/equipment/derive`` for a rig that is not a session yet -- so
    the focal ratio, resolving power and sampling the Specifications panel
    shows are the server's arithmetic, not a second copy of it in the browser.

    The planning form no longer shows any of this. It did, as a grid of tiles
    beside the inputs, and they were the densest run of uninterpreted figures
    on the page: nobody re-reads their own Dawes limit before a session.
    """

    id: str
    name: str
    telescope_id: str | None
    telescope_name: str | None
    camera_id: str | None
    camera_name: str | None

    # optics
    aperture_mm: float
    native_focal_length_mm: float
    reducer: float
    focal_length_mm: float
    """EFFECTIVE: native focal length times the reducer. Everything below uses it."""
    focal_ratio: float
    central_obstruction_mm: float
    obstruction_fraction: float
    """Obstruction diameter over aperture diameter. By AREA it is this squared."""
    collecting_area_cm2: float
    throughput: float

    # resolving power
    dawes_limit_arcsec: float
    """116/D: the double-star separation an observer can just split by eye."""
    rayleigh_limit_arcsec: float
    """1.22 lambda/D at 550 nm: the first dark ring of the Airy pattern."""
    diffraction_fwhm_arcsec: float
    """1.03 lambda/D at 550 nm: the Airy core's width. THIS is the one the
    exposure calculator uses -- it is added in quadrature to the seeing."""
    reference_seeing_arcsec: float
    psf_fwhm_arcsec: float
    """Star size at the reference seeing: hypot(seeing, diffraction FWHM)."""
    sampling_px_per_fwhm: float
    sampling: str
    """undersampled | well-sampled | oversampled, at the reference seeing."""

    # camera
    pixel_size_um: float
    sensor_width_px: int
    sensor_height_px: int
    pixel_scale_arcsec: float
    fov_width_deg: float
    fov_height_deg: float
    quantum_efficiency: float
    read_noise_e: float
    dark_current_e_per_s: float
    readout_s: float

    # mount
    switch_minutes: float
    min_block_minutes: float


class TelescopePresetOut(Api):
    id: str
    name: str
    manufacturer: str
    design: str
    """refractor | sct | aplanatic-sct | rasa | newtonian | ritchey-chretien | other"""
    aperture_mm: float
    focal_length_mm: float
    """Native. A preset for a reduced configuration carries ``reducer`` < 1."""
    reducer: float
    focal_ratio: float
    """Effective, i.e. including ``reducer``."""
    central_obstruction_mm: float
    throughput: float
    dawes_limit_arcsec: float
    notes: str
    """Where the figures came from, and which of them are estimates."""


class CameraPresetOut(Api):
    id: str
    name: str
    sensor: str
    color: bool
    pixel_size_um: float
    sensor_width_px: int
    sensor_height_px: int
    quantum_efficiency: float
    """Effective V-band. For a colour sensor this is well below ``peakQe``."""
    peak_qe: float
    read_noise_e: float
    dark_current_e_per_s: float
    readout_s: float
    notes: str


class MountOut(Api):
    switch_minutes: float
    min_block_minutes: float


class PresetsOut(Api):
    sites: list[SitePresetOut]
    equipment: list[EquipmentOut]
    """Complete rigs, addressable by ``equipmentId``."""
    telescopes: list[TelescopePresetOut]
    cameras: list[CameraPresetOut]
    default_telescope_id: str
    default_camera_id: str
    default_mount: MountOut
    default_target_ids: list[str]
    """Used when a session request names no targets at all."""
    catalog_size: int
    catalog_attribution: str


# --------------------------------------------------------------------------
# session + night
# --------------------------------------------------------------------------


class GridOut(Api):
    start: datetime
    end: datetime
    slot_minutes: int
    slot_seconds: float
    n_slots: int
    slot_starts: list[datetime]
    """Slot BOUNDARIES. Correct for drawing a timeline; wrong for animating."""
    slot_mids: list[datetime]
    """Slot MIDPOINTS -- the instants the physics was actually evaluated at.

    Every per-slot array on this API (altitude, azimuth, the frame rotation,
    the Moon) is sampled here, not at ``slotStarts``. At five-minute slots the
    two differ by 150 seconds, which is enough to visibly desynchronise an
    animation from the numbers beside it and far too little for anyone to
    notice by eye that it is wrong. Naming both axes is cheaper than the
    afternoon spent finding that out.
    """


class TwilightBandOut(Api):
    kind: str
    """day | civil | nautical | astronomical | night"""
    from_slot: int
    to_slot: int
    starts_at: datetime
    ends_at: datetime


class MoonOut(Api):
    altitude_deg: list[float]
    azimuth_deg: list[float]
    illumination: list[float]
    phase_angle_deg: list[float]
    rise_slot: int | None
    set_slot: int | None
    max_illumination: float


class GeometryRowOut(Api):
    """Per target, as_of-independent. Byte-identical at every decision point."""

    target_id: str
    name: str
    ra_deg: float
    dec_deg: float
    altitude_deg: list[float]
    azimuth_deg: list[float]
    airmass: list[float]
    moon_separation_deg: list[float]
    visible: list[bool]


class GeometryOut(Api):
    """Where everything is, all night -- served once and cached forever.

    Five of the seven arrays the heatmap used to carry never change between
    decision points, because geometry does not depend on the weather. Serving
    them from ``/grid`` meant refetching a quarter-megabyte of identical
    numbers on every scrub past a forecast run. They live here instead, behind
    an ETag, and ``/grid`` carries only what the weather decides.
    """

    session_id: str
    geometry_id: str
    """Content hash. Also the ETag, so a revalidation costs a 304."""
    n_slots: int
    slot_mids: list[datetime]
    lst_hours: list[float]
    """Local apparent sidereal time. For DISPLAY. Do not build a frame from
    it -- see ``frameQuat``."""
    frame_quat: list[list[float]]
    """Per slot, the rotation from the observer's horizon basis into ICRS, as
    ``[x, y, z, w]``.

    The horizon basis is right-handed ``(East, North, Up)``. A target's ICRS
    unit vector ``v`` has local components ``conjugate(q) * v * q``, whose Up
    component is ``sin(altitude)``.

    This is here so the browser does no astronomy whatsoever. Deriving it in
    the client from sidereal time and latitude is the obvious move and it is
    wrong by 0.32 degrees in 2026, because our coordinates are ICRS and that
    construction is only valid in apparent coordinates of date. Nineteen
    arcminutes of quietly rotated sky renders perfectly plausibly.
    """
    sun_altitude_deg: list[float]
    sun_azimuth_deg: list[float]
    moon: MoonOut
    rows: list[GeometryRowOut]


class TargetOut(Api):
    id: str
    name: str
    ra_deg: float
    dec_deg: float
    ra: str
    dec: str
    magnitude: float
    """V surface brightness in mag/arcsec^2, or a TOTAL V magnitude when
    ``isPointSource``. Two different quantities under one name because the
    scheduler reads one field; the flag is how a caller knows which it got."""
    is_point_source: bool = False
    """True only for a star. Deep-sky objects and the planets are surfaces."""
    priority: float
    urgency: float
    snr_goal: float
    required_ref_minutes: float
    """E_t in reference-minutes: how much perfect-condition time this target needs."""
    visible_slots: int
    max_altitude_deg: float
    transit_slot: int | None
    rise_slot: int | None
    """First slot above the true horizon. ``None`` means it was already up
    at dusk, or never rose -- ``visibleSlots`` distinguishes the two."""
    set_slot: int | None
    first_usable_slot: int | None
    """First slot that clears the altitude floor AND the lunar exclusion.

    Different from ``riseSlot``, and the difference is the point: a target can
    be up for hours and schedulable for none of them, which is exactly the
    question an observer is asking when they click on it."""
    last_usable_slot: int | None


class DecisionPointOut(Api):
    index: int
    at: datetime
    kind: Literal["start", "weather", "amend"]
    """What arrived: ``start`` is the opening plan, ``weather`` a forecast run,
    ``amend`` the observer adding a target. The UI colours an alert by this
    before anyone reads ``reason``. Transient alerts get their own kind when
    the ALeRCE provider lands."""
    reason: str
    plan_id: str
    records_known: int
    newest_published: datetime | None
    changed_slots: int
    added_targets: list[str]
    dropped_targets: list[str]


class NightWindowOut(Api):
    """Twilight boundaries, so the setup form never computes astronomy."""

    date: str
    sunset: datetime | None
    dusk: datetime | None
    dawn: datetime | None
    sunrise: datetime | None
    dark_hours: float
    utc_offset_hours: float
    """Mean SOLAR offset from longitude -- NOT a civil timezone, and labelled
    as such wherever it is shown. A civil offset needs a tz-boundary database
    and a DST table, both of which are wrong at exactly the moments that
    matter; solar offset is exact and is what actually governs darkness."""
    always_up: bool
    never_rises: bool
    suggested_start: datetime
    suggested_hours: float


class WeatherSourceOut(Api):
    """Where this night's weather came from, and how far it can be trusted.

    Resolved once, when the session is created, from when the night is
    relative to the wall clock -- the one decision in the weather path that
    legitimately needs "now". Every plan built from it still applies the
    publication gate at its own as_of.
    """

    requested: str
    """auto | synthetic | open_meteo"""
    mode: str
    """archive  -- the night is over; archived model runs, replayed by publication time
    forecast -- tonight or a coming night; runs published so far plus the live forecast
    synthetic -- offline demo data, labelled as such
    unavailable -- no forecast covers this night (too far ahead, before the archive,
                   or the weather service could not be reached); the plan assumes clear"""
    label: str
    description: str
    model: str | None
    fetched_at: datetime | None
    """When the live forecast was retrieved. None unless ``mode`` is forecast."""
    runs: int
    """Distinct model runs that reached at least one plan."""
    records: int


class OutlookNoteOut(Api):
    severity: str
    """info | warn | critical"""
    text: str


class OutlookWindowOut(Api):
    starts_at: datetime
    ends_at: datetime
    hours: float
    mean_cloud_fraction: float


class WeatherOutlookOut(Api):
    """A plain-language read of the forecast, as it stood at this plan's as_of.

    Lives on the PLAN rather than the session so it obeys the same rule as
    everything else: scrub back to dusk and it says what was knowable at dusk.
    Summarises only the dark part of the window, because cloud at 7 p.m. in
    civil twilight is not what anyone setting up a telescope is asking about.
    """

    as_of: datetime
    has_data: bool
    """False when no forecast record reached this plan -- then every number
    below describes an ASSUMED clear sky, and ``headline`` says so."""
    basis: str
    """e.g. "ECMWF IFS run of 12:00 UTC, published 19:30 UTC"."""
    category: str
    """clear | mostly-clear | partly-cloudy | mostly-cloudy | overcast | unknown
    -- the NWS sky-cover terms, by mean cover in eighths (oktas)."""
    label: str
    headline: str
    verdict: str
    """go | marginal | poor | unknown"""
    verdict_text: str
    dark_hours: float
    clear_hours: float
    """Dark hours forecast under the clear threshold (30% cover)."""
    mean_cloud_fraction: float
    best_window: OutlookWindowOut | None
    """The longest clear stretch inside the dark hours, if there is one."""
    humidity_max: float | None
    dew_spread_min_c: float | None
    wind_max_ms: float | None
    notes: list[OutlookNoteOut]
    """Trend, dew, wind and data caveats, each with something to do about it."""


class LiveOut(Api):
    """How the server keeps a night that is still happening current.

    Present only for a night that had not ended when the session was created.
    Such a night cannot be folded once: the data that will decide its second
    half has not been published. So the server WATCHES it -- every
    ``every_minutes`` it asks the weather source whether anything new exists,
    and when something does it re-plans at the moment it learned it and
    appends that as a decision point. These fields exist so the observer can
    see the watch working rather than take it on trust.
    """

    following: bool
    """True until the night ends. Then the record is complete and replayable."""
    every_minutes: float | None
    """None when scheduled checks are off; ``POST .../refresh`` still works."""
    checks: int
    """Checks made since the session was created, scheduled or manual."""
    updates: int
    """Checks that found new data and re-planned."""
    revision: int
    """Bumped whenever a decision point is added or replaced. Clients key their
    plan and grid caches on it: before dusk the opening plan itself can be
    re-made, which changes a plan without changing how many there are."""
    last_checked_at: datetime | None
    next_check_at: datetime | None
    last_result: str
    """What the last check found, in words: "no new ECMWF IFS 0.25° run since
    the 18Z run", "new forecast fetched 01:12 UTC; the plan changed"."""
    last_error: str | None
    """Why the last check could not ask, when it could not (e.g. HTTP 429)."""


class SessionOut(Api):
    id: str
    name: str
    status: str
    """building | ready | failed"""
    progress: float
    message: str
    created_at: datetime
    site: SitePresetOut
    equipment: EquipmentOut
    grid: GridOut
    targets: list[TargetOut]
    weather_source: str
    """The requested source, verbatim. Kept for older clients; read ``weather``."""
    weather: WeatherSourceOut
    sun_altitude_deg: list[float]
    twilight: list[TwilightBandOut]
    moon: MoonOut
    decision_points: list[DecisionPointOut]
    distinct_plans: int
    fold_seconds: float
    error: str | None = None
    live: LiveOut | None = None


class SessionListItemOut(Api):
    id: str
    name: str
    status: str
    created_at: datetime
    night_start: datetime
    n_targets: int
    decision_points: int


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


class WarningOut(Api):
    severity: str
    """critical | warn | info"""
    category: Literal["weather", "moon", "horizon", "exposure"]
    """What KIND of hazard it is, as against how much it matters. Two questions,
    two fields: a cloud bank and a short frame count need different reactions
    at the same severity."""
    text: str
    action: str


class BlockOut(Api):
    target_id: str
    name: str
    slot_start: int
    slot_end: int
    starts_at: datetime
    ends_at: datetime
    integrating_from: datetime
    switch_minutes: float
    integrating_minutes: float

    ra_deg: float
    dec_deg: float
    ra: str
    dec: str

    altitude_start_deg: float
    altitude_end_deg: float
    altitude_min_deg: float
    altitude_max_deg: float
    azimuth_start_deg: float
    azimuth_end_deg: float
    compass: str
    airmass_min: float
    airmass_max: float
    moon_separation_min_deg: float

    n_subs: int
    t_sub_s: float
    expected_snr: float
    """SNR from this block alone. Blocks combine in QUADRATURE, not linearly."""
    snr_goal: float
    mean_efficiency: float
    mean_preference: float
    mean_cloud_fraction: float
    warnings: list[WarningOut]


class SlotOut(Api):
    slot: int
    kind: str
    target_id: str | None
    locked: bool


class DroppedOut(Api):
    target_id: str
    name: str
    code: str
    message: str


class EvidenceOut(Api):
    records: int
    sources: list[str]
    newest_published: datetime | None
    estimated_fraction: float
    has_oracle: bool


class TargetProgressOut(Api):
    target_id: str
    name: str
    included: bool
    accumulated_ref_minutes: float
    required_ref_minutes: float
    fraction: float
    expected_snr: float
    snr_goal: float
    observing_slots: int


class ChangesOut(Api):
    from_plan_id: str | None
    locked_through_slot: int
    changed_slots: int
    added_targets: list[str]
    dropped_targets: list[str]
    past_slots_rewritten: int
    """Must always be zero. Surfaced rather than asserted away so the guarantee
    is visible in the payload the UI renders, not just in a test."""
    headline: str


class SlotWeatherOut(Api):
    """The forecast in force at this plan's decision point, one value per slot
    (aligned with ``PlanOut.slots``), for everything the source forecasts
    beyond cloud. ``None`` means the source did not say -- never a guess.
    Cloud totals and seeing are ``PlanOut.cloudFraction`` / ``seeingFwhmArcsec``.
    """

    temperature_c: list[float | None] = Field(default_factory=list)
    dew_point_c: list[float | None] = Field(default_factory=list)
    humidity: list[float | None] = Field(default_factory=list)
    """Relative humidity at 2 m, 0-1."""
    wind_ms: list[float | None] = Field(default_factory=list)
    wind_gust_ms: list[float | None] = Field(default_factory=list)
    precipitation_mm: list[float | None] = Field(default_factory=list)
    """Per hour."""
    cloud_low: list[float | None] = Field(default_factory=list)
    cloud_mid: list[float | None] = Field(default_factory=list)
    cloud_high: list[float | None] = Field(default_factory=list)
    """Cloud layers, 0-1 each; the total is ``PlanOut.cloudFraction``."""
    covered: list[bool] = Field(default_factory=list)
    """True where a forecast reaches the slot. False where none does (no
    forecast at all, or past the forecast's last hour): there
    ``cloudFraction`` is the plan's clear-sky ASSUMPTION, not a forecast, and
    every array above is ``None``."""
    seeing_forecast: bool = False
    """False: no source forecasts seeing, and ``seeingFwhmArcsec`` is the
    planning assumption, to be labelled as such."""
    basis: str | None = None
    """Which run these values come from, in words (the outlook's basis)."""


class PlanOut(Api):
    session_id: str
    plan_id: str
    decision_index: int
    decision_count: int
    as_of: datetime
    valid_from: datetime
    valid_until: datetime
    reason: str

    status: str
    objective: float
    gap: float
    solve_ms: float
    summary: str

    blocks: list[BlockOut]
    slots: list[SlotOut]
    dropped: list[DroppedOut]
    progress: list[TargetProgressOut]
    cloud_fraction: list[float]
    seeing_fwhm_arcsec: list[float]
    evidence: EvidenceOut
    changes: ChangesOut
    outlook: WeatherOutlookOut
    slot_weather: SlotWeatherOut = Field(default_factory=SlotWeatherOut)


# --------------------------------------------------------------------------
# tonight -- the catalogue, ranked for one site, night and rig
# --------------------------------------------------------------------------


class TargetSuggestionOut(Api):
    """One catalogue object, and what tonight holds for it.

    Positions come from the same per-slot horizon rotation the globe is drawn
    with, applied to the whole catalogue at once; they agree with a session's
    astropy geometry to about an arcminute, which is ample for ranking and is
    not what the scheduler plans from -- a chosen target is recomputed exactly
    when the session is built.
    """

    id: str
    """Canonical and stable: ``m31``, ``ngc7000``, ``ic1396``, ``sh2-101``."""
    name: str
    """Common name when there is one, otherwise the designation."""
    designation: str
    """Every catalogue number, most familiar first: "M 31 · NGC 224"."""
    common_name: str | None
    search_text: str
    """Lower-case names, numbers (with and without spaces), type and
    constellation, so the client can match a query with a substring test and
    never needs its own alias table."""
    type: str
    """galaxy | emission-nebula | reflection-nebula | planetary-nebula |
    supernova-remnant | open-cluster | globular-cluster | cluster-nebula | other"""
    type_label: str
    constellation: str
    ra_deg: float
    dec_deg: float
    ra: str
    dec: str
    major_arcmin: float | None
    minor_arcmin: float | None
    v_mag: float | None
    """Integrated V magnitude, where the catalogue has one."""
    magnitude: float
    """V SURFACE brightness in mag/arcsec^2 -- what the exposure calculator
    uses, under the same name ``TargetOut`` gives it."""
    magnitude_estimated: bool
    """True when there was no catalogue magnitude to derive it from and a
    documented typical value for the object type stands in."""
    messier: int | None
    caldwell: int | None
    plannable: bool
    """False for what the exposure calculator cannot model -- a double star,
    an asterism, a dark nebula (a deficit, not a source), or a Caldwell number
    that names two objects. Such entries are searchable so a search for them
    explains itself, but they are never recommended and a session refuses
    them. ``whyNot`` says which it is."""

    visible: bool
    """Usable for at least one sample tonight: dark, above the site's altitude
    floor, and outside the lunar exclusion."""
    usable_hours: float
    max_altitude_deg: float
    """Highest point while the sky is dark, moonlit or not."""
    best_altitude_deg: float | None = None
    """Altitude at ``bestAt``: the highest USABLE point, after the lunar
    exclusion. Differs from ``maxAltitudeDeg`` only when the Moon hides the
    peak. None when there is no usable moment."""
    best_at: datetime | None
    usable_from: datetime | None
    usable_until: datetime | None
    transit_at: datetime | None
    moon_separation_deg: float | None
    """At ``bestAt``; None when there is no usable moment."""
    why_not: str | None
    """For an invisible object, the reason in words: never clears the floor,
    only up in daylight, or lost in moonlight."""

    framing: str
    """fits | tight | mosaic | small -- the object against this rig's field."""
    framing_fill: float | None
    """Major axis over the field's SHORT side. None without a catalogued size."""
    hours_to_goal: float | None
    """Imaging hours to the SNR goal with this rig: open shutter plus the
    camera's download between frames, as plans count them. Priced at the
    usable sample where it is quickest -- that sample's altitude, and this
    site's zenith sky plus that moment's twilight -- which is usually, but
    not always, ``bestAt``. Ignores cloud and moonlight -- it is the best
    case, and is labelled as such where shown."""
    feasible: bool
    """``hoursToGoal`` fits inside ``usableHours``."""
    score: float
    """0-1, the ranking key for recommendations. Not a physical quantity."""
    reasons: list[str]
    """Short, specific phrases behind the score: "5.2 h above 30°",
    "transits 23:40 at 71°", "fills 45% of the frame"."""


class TonightOut(Api):
    date: str
    site_name: str
    window_start: datetime
    window_end: datetime
    dark_hours: float
    """Hours of the window with the Sun at least 18° down: astronomical
    darkness, as ``/api/night-window`` counts it. When the Sun never gets that
    low in the window, the hours with it at least 12° down instead -- the
    nautical twilight the ranking then treats as dark -- and ``rankingNote``
    says so. ``usableHours`` always counts from 12° down, so on a window that
    reaches into nautical twilight it can exceed this."""
    moon_illumination: float
    moon_up_hours: float
    fov_width_deg: float
    fov_height_deg: float
    catalog_size: int
    visible_count: int
    recommended_ids: list[str]
    """The best few, in order. Every one is visible tonight."""
    targets: list[TargetSuggestionOut]
    """The WHOLE catalogue, best first, invisible objects last -- so a search
    for something that is not up tonight finds it and says why."""
    attribution: str
    ranking_note: str
    """One sentence on how the ranking is made, for the UI to show verbatim."""


# --------------------------------------------------------------------------
# quality grid (the heatmap)
# --------------------------------------------------------------------------


class GridRowOut(Api):
    """Per target, what the WEATHER decides. Geometry is on ``/geometry``."""

    target_id: str
    name: str
    efficiency: list[float]
    """Physically calibrated. 0.4 means one second here buys 0.4 reference-
    seconds of progress. Drives feasibility."""
    preference: list[float]
    """Soft policy, the product of ``preferenceFactors``. Drives only the
    objective. Never blend this with efficiency for display: the whole point
    of the split is that one says what is possible and the other what is
    nice."""
    preference_factors: dict[str, list[float]]
    """altitude | moon | cloud | seeing | satellite. Because preference is a
    product, the per-factor log difference between two decision points is an
    exact attribution of why a slot changed."""
    sky_mag_arcsec2: list[float]
    sky_components_nl: dict[str, list[float]]
    """natural | artificial | moon | twilight | cloudDelta, in nanoLamberts.

    Flux, not magnitudes, because these SUM -- adding magnitudes is the
    canonical bug in this domain. ``cloudDelta`` is legitimately negative at a
    pristine site, where cloud darkens the sky instead of amplifying city
    glow, so a renderer must not assume these are non-negative.
    """


class QualityGridOut(Api):
    session_id: str
    as_of: datetime
    plan_id: str
    n_slots: int
    rows: list[GridRowOut]
    cloud_fraction: list[float]
    seeing_fwhm_arcsec: list[float]


class PlanetOut(Api):
    id: str
    name: str
    altitude_deg: list[float]
    """Per slot midpoint, like the Moon's."""
    azimuth_deg: list[float]
    ra_deg: float
    """Apparent place at the night's middle slot."""
    dec_deg: float
    ra: str
    dec: str
    magnitude: float
    """Astronomical Almanac phase law at the night's middle slot."""
    distance_au: float
    phase_angle_deg: float


class SkyOut(Api):
    """The sky around the plan: planets, and how dark it is.

    Display only -- nothing here reaches a plan -- and weather-independent, so
    like ``/geometry`` it takes no ``as_of`` and is served once per session.
    """

    session_id: str
    n_slots: int
    planets: list[PlanetOut]
    zenith_sky_mag_arcsec2: list[float]
    """Per slot, cloud-free: natural + artificial + moonlight + twilight at the
    zenith, from the same sky model the scheduler uses."""
    naked_eye_limit_mag: list[float]
    """Per slot, the faintest star visible at the zenith (Schaefer 1990). The
    sky view draws stars to this limit, so a bright Moon thins the picture the
    way it thins the real sky."""


class SatellitePassOut(Api):
    """One satellite's continuous stretch above the horizon, every ``stepSeconds``."""

    norad_id: int
    name: str
    starts_at: datetime
    step_seconds: float
    altitude_deg: list[float]
    azimuth_deg: list[float]
    magnitude: list[float | None]
    """Estimated V magnitude; null while the satellite is in Earth's shadow and
    therefore invisible."""
    range_km: list[float]


class SatellitesOut(Api):
    """Satellites crossing this night's sky. Display only -- see ``/sky``.

    Elements are the CURRENT CelesTrak set, propagated to the night. That is
    exact for tonight and an increasingly rough estimate for a replay days in
    the past, which is why the provenance travels with the payload.
    """

    session_id: str
    available: bool
    reason: str | None = None
    """Why nothing is shown, when ``available`` is false."""
    source: str
    elements_published_at: datetime | None
    epoch_spread_days: float
    """Largest gap between an element set's epoch and the night."""
    n_objects: int
    step_seconds: float
    passes: list[SatellitePassOut]


# --------------------------------------------------------------------------
# amending a planned night, and the catalogue the sky draws to amend from
# --------------------------------------------------------------------------


class AddTargetRequest(Api):
    """Add one object to a night that is already planned.

    Usually a name the server can resolve. It may instead carry its OWN
    coordinates, which is the only way to add something the server has never
    heard of -- a star. All 41,411 of them live in the browser, in
    ``public/sky/stars.bin``, keyed by position in that file, and some 36,000
    have no name at all; there is nothing for the server to look up. So the
    client sends what it knows, exactly as ``SessionRequest.targets`` already
    allows for a custom object.
    """

    target: str = Field(
        description=(
            "anything that names a catalogue object or a planet: m31, 'NGC 7000', "
            "'Horsehead', 'jupiter'. With raDeg, decDeg and magnitude it is instead "
            "this object's id, and nothing is looked up"
        )
    )
    ra_deg: float | None = Field(default=None, ge=0.0, lt=360.0)
    dec_deg: float | None = Field(default=None, ge=-90.0, le=90.0)
    magnitude: float | None = Field(
        default=None,
        description=(
            "V surface brightness in mag/arcsec^2, or a TOTAL V magnitude when "
            "isPointSource is true. All three of raDeg, decDeg and magnitude are "
            "needed together"
        ),
    )
    name: str | None = Field(default=None, description="display label; defaults to the id")
    is_point_source: bool = Field(
        default=False,
        description=(
            "true for a star: magnitude is then its total light, and the exposure "
            "model prices it as a point source instead of a surface. Getting this "
            "wrong is a factor of ~25 in exposure time"
        ),
    )
    at: datetime | None = Field(
        default=None,
        description=(
            "a replay re-plans from this instant (the cursor); omitted means dusk. "
            "Ignored for a night still happening, which is always amended from now"
        ),
    )


class AmendOut(Api):
    """What adding the object did.

    Only ever returned for an object that IS scheduled: adding a target is a
    request, not an order, and one the optimiser cannot place is refused with
    409 and the reason rather than joining the night unscheduled.
    """

    session_id: str
    target_id: str
    target_name: str
    at: datetime
    """When the amended plan takes over."""
    decision_index: int
    message: str
    """Where the amended plan gives it time, in words."""


class CatalogObjectOut(Api):
    """One catalogue object, for drawing and picking in the sky."""

    id: str
    name: str
    designation: str
    type: str
    type_label: str
    constellation: str
    ra_deg: float
    dec_deg: float
    ra: str
    dec: str
    major_arcmin: float | None
    v_mag: float | None
    magnitude: float
    """V SURFACE brightness in mag/arcsec^2, as ``TargetOut.magnitude``."""
    plannable: bool
    why_not: str | None
    """Why it cannot be planned, when it cannot."""


class CatalogOut(Api):
    size: int
    objects: list[CatalogObjectOut]
    attribution: str


class HealthOut(Api):
    status: str
    version: str
    sessions: int
    settings: dict[str, str | bool | int | float]


# --------------------------------------------------------------------------
# the whole night, in one response
# --------------------------------------------------------------------------


class DecisionPlanOut(Api):
    """One decision point with everything that depends on it.

    ``plan`` and ``grid`` are exactly what ``/plan?as_of=`` and ``/grid?as_of=``
    answer for this point's instant. They are carried together because the
    client can no longer ask for one later: there is no session on the server
    to ask about.
    """

    index: int
    at: datetime
    plan: PlanOut
    grid: QualityGridOut


class FullNightOut(Api):
    """A folded night, complete, with nothing left on the server.

    There is no session id here, and its absence is the point: an id is a
    handle on state, and no state was kept. Everything the UI can ask about
    this night is in this object, so a scrub is a lookup in ``decisions``
    rather than a request, and a refresh is this same POST sent again.

    ``session.decisionPoints`` and ``decisions`` are the same points in the
    same order -- the first carries the timeline the slider snaps to, the
    second the plans those instants resolve to.
    """

    session: SessionOut
    geometry: GeometryOut
    sky: SkyOut
    satellites: SatellitesOut
    decisions: list[DecisionPlanOut]
