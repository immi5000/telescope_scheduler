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

from pydantic import BaseModel, ConfigDict, Field
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


class TargetRequest(Api):
    id: str
    name: str | None = None
    ra_deg: float | None = Field(default=None, ge=0, lt=360)
    dec_deg: float | None = Field(default=None, ge=-90, le=90)
    magnitude: float | None = Field(default=None, description="V surface brightness, mag/arcsec^2")
    priority: float = Field(default=1.0, gt=0)
    urgency: float = Field(default=1.0, gt=0)
    snr_goal: float | None = Field(default=None, gt=0)


class SessionRequest(Api):
    name: str | None = None
    date: str = Field(description="night start date, UTC, YYYY-MM-DD")
    start_hour_utc: float = Field(default=1.0, ge=0, lt=24)
    hours: float = Field(default=8.0, gt=0, le=16)
    slot_minutes: int = Field(default=5, ge=1, le=30)

    site_id: str | None = "urbana"
    site: SiteRequest | None = Field(default=None, description="overrides siteId when present")
    equipment_id: str = "sct8-2600mm"

    snr_goal: float = Field(default=60.0, gt=0)
    t_sub_s: float = Field(default=90.0, gt=0)
    targets: list[TargetRequest] | None = None

    weather: str = Field(
        default="synthetic",
        description="'synthetic' (offline, deterministic) or 'open_meteo' (real archived runs)",
    )
    solve_seconds: float = Field(default=4.0, gt=0, le=60)


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------


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


class EquipmentPresetOut(Api):
    id: str
    name: str
    aperture_mm: float
    focal_length_mm: float
    focal_ratio: float
    collecting_area_cm2: float
    throughput: float
    pixel_size_um: float
    pixel_scale_arcsec: float
    fov_width_deg: float
    fov_height_deg: float
    read_noise_e: float
    dark_current_e_per_s: float
    quantum_efficiency: float
    switch_minutes: float
    min_block_minutes: float


class CatalogEntryOut(Api):
    id: str
    name: str
    ra_deg: float
    dec_deg: float
    ra: str
    dec: str
    magnitude: float


class PresetsOut(Api):
    sites: list[SitePresetOut]
    equipment: list[EquipmentPresetOut]
    catalog: list[CatalogEntryOut]
    default_target_ids: list[str]


# --------------------------------------------------------------------------
# session + night
# --------------------------------------------------------------------------


class GridOut(Api):
    start: datetime
    end: datetime
    slot_minutes: int
    n_slots: int
    slot_starts: list[datetime]


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


class TargetOut(Api):
    id: str
    name: str
    ra_deg: float
    dec_deg: float
    ra: str
    dec: str
    magnitude: float
    priority: float
    urgency: float
    snr_goal: float
    required_ref_minutes: float
    """E_t in reference-minutes: how much perfect-condition time this target needs."""
    visible_slots: int
    max_altitude_deg: float
    transit_slot: int | None


class DecisionPointOut(Api):
    index: int
    at: datetime
    reason: str
    plan_id: str
    records_known: int
    newest_published: datetime | None
    changed_slots: int
    added_targets: list[str]
    dropped_targets: list[str]


class SessionOut(Api):
    id: str
    name: str
    status: str
    """building | ready | failed"""
    progress: float
    message: str
    created_at: datetime
    site: SitePresetOut
    equipment: EquipmentPresetOut
    grid: GridOut
    targets: list[TargetOut]
    weather_source: str
    sun_altitude_deg: list[float]
    twilight: list[TwilightBandOut]
    moon: MoonOut
    decision_points: list[DecisionPointOut]
    distinct_plans: int
    fold_seconds: float
    error: str | None = None


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


# --------------------------------------------------------------------------
# quality grid (the heatmap)
# --------------------------------------------------------------------------


class GridRowOut(Api):
    target_id: str
    name: str
    efficiency: list[float]
    preference: list[float]
    altitude_deg: list[float]
    airmass: list[float]
    moon_separation_deg: list[float]
    visible: list[bool]


class QualityGridOut(Api):
    session_id: str
    as_of: datetime
    plan_id: str
    n_slots: int
    rows: list[GridRowOut]
    cloud_fraction: list[float]
    seeing_fwhm_arcsec: list[float]


class HealthOut(Api):
    status: str
    version: str
    sessions: int
    settings: dict[str, str | bool | int | float]
