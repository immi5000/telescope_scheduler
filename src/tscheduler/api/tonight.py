"""What is worth imaging tonight: the catalogue, ranked for a site, night and rig.

Three questions, answered for every object at once:

  IS IT UP?      Hours above the site's altitude floor while the sky is dark,
                 outside the lunar exclusion -- the same two vetoes the
                 scheduler applies, from the bulk survey in physics/survey.py.
  CAN I FINISH?  Imaging hours to the SNR goal with THIS rig from THIS
                 site's sky -- open shutter plus the camera's download
                 between frames, as plans count them -- at whichever usable
                 moment gets there fastest: its altitude then, and the
                 twilight then. The same extended-source calculator the
                 scheduler plans with, so a target shown as "2.1 h" is one
                 the planner also thinks needs about that long -- before
                 cloud and moonlight.
  DOES IT FIT?   Its size against the camera's field.

The weather is deliberately absent. This answers "what is up and worth it";
the plan answers "and will it be clear", from data with a publication time.

``score`` orders the recommendations and is not a physical quantity. The
``reasons`` beside it are, and they are what the UI shows.

Performance: the catalogue is ~1,850 objects. Everything that depends only on
the catalogue is computed once per process (``_static``); everything that
depends on the site and window, once per site and window (``_survey``); and
what depends on the rig is numpy over the whole catalogue. The only per-object
Python left is building the response rows.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from tscheduler import catalog
from tscheduler.api import nightwindow, schemas
from tscheduler.api.equipment import EquipmentPreset
from tscheduler.api.presets import CATALOG_ATTRIBUTION, SitePreset
from tscheduler.catalog import DeepSkyObject
from tscheduler.domain.site import Site
from tscheduler.physics.airmass import airmass_kasten_young
from tscheduler.physics.convert import mag_arcsec2_to_nl, nl_to_mag_arcsec2
from tscheduler.physics.quality import extended_snr2_rate, resolution_element
from tscheduler.physics.survey import NightSurvey, survey_night
from tscheduler.physics.twilight import twilight_sky_nl
from tscheduler.pipeline.conditions import download_duty_cycle

#: Survey sampling. Ten minutes resolves rise, set and transit to well inside
#: anything a person choosing targets cares about, and keeps the astropy half
#: to a few dozen instants. A window shorter than this is one sample of its
#: own length (``_sample_seconds``).
SAMPLE_MINUTES = 10

#: The Sun must be at least this far down for a sample to count as usable.
#: Nautical rather than astronomical, because a user who narrows the window
#: into twilight has chosen to image there -- the plan prices twilight in
#: through the sky model; this only refuses the sky that is plainly not dark.
DARK_SUN_ALT_DEG = -12.0

#: Astronomical darkness, which is what ``darkHours`` counts -- the same
#: boundary /api/night-window's dusk and dawn use.
ASTRO_DARK_SUN_ALT_DEG = nightwindow.DARK_ALT_DEG

#: How many recommendations to name. The rest of the catalogue is still in
#: ``targets``, ranked, for the search box.
N_RECOMMENDED = 12

#: Framing bands. ``framingFill`` is the major axis over the field's SHORT
#: side, as the wire promises; whether an object needs a mosaic is judged with
#: the camera turned to put its long side along the object's major axis,
#: because that is what anyone framing an elongated target does.
SMALL_BELOW = 0.10
TIGHT_ABOVE = 0.80

#: A candidate for recommendation must be usable for at least this long (or
#: for half the dark hours, on a short night). Ten minutes above the floor is
#: "technically visible", not a suggestion.
MIN_RECOMMEND_HOURS = 1.0

#: Usable hours that earn full credit for time. See the score.
FULL_CREDIT_HOURS = 6.0

#: Pixels across (major axis) at which an object is a thumbnail, and at which
#: it is a full image. See ``_framing_factor``.
SPAN_PX = (30.0, 600.0)

#: Keeping the picks varied: each pick from a family (galaxies, nebulae,
#: planetaries, clusters) multiplies the next one's score by FAMILY_DECAY,
#: and no family takes more than FAMILY_CAP of the list. Gentle on purpose --
#: in September the good targets for a small refractor really are mostly
#: nebulae, and the list should say so rather than pad itself with clusters.
FAMILY_DECAY = 0.97
FAMILY_CAP = 7
#: An object that cannot reach the SNR goal tonight is already marked down
#: by ``_reach``; this is the extra nudge that makes "prefer feasible" hold
#: between two otherwise equal picks.
INFEASIBLE_FACTOR = 0.95

Candidate = DeepSkyObject
"""The ranking works on catalogue objects directly."""


def candidates() -> tuple[DeepSkyObject, ...]:
    """Every catalogue object, plannable or not, in the data file's order."""
    return catalog.objects()


# --------------------------------------------------------------------------
# rig- and night-independent arrays
# --------------------------------------------------------------------------

#: Type families, for keeping the recommendations varied.
_FAMILY = {
    "galaxy": 0,
    "emission-nebula": 1,
    "reflection-nebula": 1,
    "supernova-remnant": 1,
    "cluster-nebula": 1,
    "planetary-nebula": 2,
    "open-cluster": 3,
    "globular-cluster": 3,
    "other": 4,
}


@dataclass(frozen=True, slots=True)
class _Static:
    objs: tuple[DeepSkyObject, ...]
    ra: NDArray[np.float64]
    dec: NDArray[np.float64]
    unit: NDArray[np.float64]
    """(N, 3) ICRS unit vectors, for same-field tests."""
    sb: NDArray[np.float64]
    major: NDArray[np.float64]
    """arcmin; NaN where the catalogue has no size."""
    minor: NDArray[np.float64]
    plannable: NDArray[np.bool_]
    appeal: NDArray[np.float64]
    family: NDArray[np.int64]
    ra_text: tuple[str, ...]
    dec_text: tuple[str, ...]


#: How much imagers go out of their way for each kind of object, other things
#: equal. Open clusters and featureless ellipticals photograph as star fields
#: and smooth ovals; nebulae, spirals and planetaries are why people buy
#: telescopes. A named object gets half of any shortfall back -- the Pleiades
#: and the Double Cluster are imaged for good reason.
_TYPE_APPEAL = {
    "galaxy": 1.0,
    "emission-nebula": 1.0,
    "reflection-nebula": 1.0,
    "supernova-remnant": 1.0,
    "cluster-nebula": 1.0,
    "planetary-nebula": 1.0,
    "globular-cluster": 0.85,
    "open-cluster": 0.55,
    "other": 0.55,
}
#: Galaxies by look: smooth ellipticals and lenticulars, and the irregular
#: dwarfs that make up most of the rest, give less back for the same hours.
_GALAXY_APPEAL = {"Elliptical galaxy": 0.85, "Lenticular galaxy": 0.85, "Irregular galaxy": 0.9}


def _appeal(o: DeepSkyObject) -> float:
    """How likely an imager is to want it: renown times kind. A tiebreak
    between objects the physics rates alike -- which, for a catalogue this
    size, is most of them -- not a verdict on the unnamed ones.

    Renown: a Messier or Caldwell object with a common name 1.0, one without
    0.9 (M110, NGC 7331), a named object 0.93, an unnamed NGC/IC 0.72, an
    unnamed Sharpless region or other 0.66.
    """
    listed = o.messier is not None or o.caldwell is not None
    if listed:
        renown = 1.0 if o.common_name else 0.9
    elif o.common_name:
        renown = 0.93
    else:
        renown = 0.72 if o.id.startswith(("ngc", "ic")) else 0.66
    kind = _TYPE_APPEAL.get(o.type, 0.7)
    if o.type == "galaxy":
        kind = _GALAXY_APPEAL.get(o.type_label, kind)
    if o.common_name:
        kind = 1.0 - (1.0 - kind) / 2.0
    return renown * kind


@functools.cache
def _static() -> _Static:
    objs = candidates()
    ra = np.array([o.ra_deg for o in objs], dtype=np.float64)
    dec = np.array([o.dec_deg for o in objs], dtype=np.float64)
    r, d = np.radians(ra), np.radians(dec)
    major = np.array([np.nan if o.major_arcmin is None else o.major_arcmin for o in objs])
    minor = np.array(
        [np.nan if o.major_arcmin is None else (o.minor_arcmin or o.major_arcmin) for o in objs]
    )
    return _Static(
        objs=objs,
        ra=ra,
        dec=dec,
        unit=np.stack([np.cos(d) * np.cos(r), np.cos(d) * np.sin(r), np.sin(d)], axis=-1),
        sb=np.array([o.surface_brightness for o in objs], dtype=np.float64),
        major=major,
        minor=minor,
        plannable=np.array([o.plannable for o in objs], dtype=np.bool_),
        appeal=np.array([_appeal(o) for o in objs], dtype=np.float64),
        family=np.array([_FAMILY.get(o.type, 4) for o in objs], dtype=np.int64),
        ra_text=tuple(_ra(o.ra_deg) for o in objs),
        dec_text=tuple(_dec(o.dec_deg) for o in objs),
    )


# --------------------------------------------------------------------------
# the night
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def _night_window(lat: float, lon: float, elev: float, date: str) -> nightwindow.NightWindow:
    return nightwindow.night_window(lat, lon, elev, date)


def _window(req: schemas.TonightRequest, site: Site) -> tuple[datetime, datetime]:
    if req.start is not None:
        start = req.start if req.start.tzinfo else req.start.replace(tzinfo=UTC)
        start = start.astimezone(UTC)
        return start, start + timedelta(hours=req.hours or 8.0)
    w = _night_window(
        round(site.latitude_deg, 5),
        round(site.longitude_deg, 5),
        round(site.elevation_m, 1),
        req.date,
    )
    dusk = w.dusk or w.sunset
    dawn = w.dawn or w.sunrise
    if dusk is None or dawn is None or dawn <= dusk:
        # Polar day or night: no dusk to anchor on. Local solar midnight,
        # four hours either side of it, is at least a defined question.
        day = datetime.strptime(req.date, "%Y-%m-%d").replace(tzinfo=UTC)
        mid = day + timedelta(hours=24 - w.utc_offset_hours)
        return mid - timedelta(hours=4), mid + timedelta(hours=4)
    if req.hours is not None:
        dawn = dusk + timedelta(hours=req.hours)
    return dusk, dawn


def _sample_seconds(start_unix: float, end_unix: float) -> float:
    """How long each survey sample stands for: ``SAMPLE_MINUTES``, or the
    whole window when it is shorter -- one sample, centred in it, so no time,
    altitude or hour count falls outside the window. The floor is one
    microsecond, the clock's resolution, not a second: a window of a fraction
    of a second (the request allows any length above zero) is still its own
    sample."""
    return min(SAMPLE_MINUTES * 60.0, max(end_unix - start_unix, 1e-6))


@functools.lru_cache(maxsize=16)
def _survey(
    lat: float, lon: float, elev: float, start_unix: float, end_unix: float
) -> tuple[NightSurvey, NDArray[np.float64], NDArray[np.float64]]:
    """The expensive, rig-independent half, cached per site and window.

    Returns the survey plus altitude and Moon separation for the whole
    catalogue, each ``(N, S)``. Samples are stamped at their midpoints.
    """
    step = _sample_seconds(start_unix, end_unix)
    n = max(int((end_unix - start_unix) // step), 1)
    mids = start_unix + step * (np.arange(n, dtype=np.float64) + 0.5)
    site = Site(latitude_deg=lat, longitude_deg=lon, elevation_m=elev)
    sv = survey_night(site, mids)
    st = _static()
    alt, _, sep = sv.place(st.ra, st.dec)
    return sv, alt, sep


# --------------------------------------------------------------------------
# framing and scoring
# --------------------------------------------------------------------------


def _framing(
    st: _Static, fov_long_arcmin: float, fov_short_arcmin: float
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], NDArray[np.object_]]:
    """``(fill, fit_long, fit_short, kind)`` for every object.

    ``fill`` is the major axis over the short side (NaN without a size), as
    the wire reports it. ``fit_long``/``fit_short`` are the major axis over
    the LONG side and the minor over the short: the camera turned to suit the
    object, which is how anyone frames an elongated target. Either over 1
    means a mosaic.
    """
    with np.errstate(invalid="ignore"):
        fill = st.major / fov_short_arcmin
        fit_long = st.major / fov_long_arcmin
        fit_short = st.minor / fov_short_arcmin
    kind = np.full(st.major.shape, "fits", dtype=object)
    sized = np.isfinite(fill)
    kind[sized & (fill < SMALL_BELOW)] = "small"
    kind[sized & (fill > TIGHT_ABOVE)] = "tight"
    kind[sized & ((fit_long > 1.0) | (fit_short > 1.0))] = "mosaic"
    return fill, fit_long, fit_short, kind


def _framing_factor(
    fit_long: NDArray[np.float64],
    fit_short: NDArray[np.float64],
    fill: NDArray[np.float64],
    span_px: NDArray[np.float64],
) -> NDArray[np.float64]:
    """How well each object suits this field and pixel scale, 0.15-1.

    Big: what a mosaic costs is panels, and panels are roughly the object's
    area in frames (camera rotated to suit). Up to two panels the factor is
    gentle -- framing the best part of NGC 7000 on a small refractor is what
    everyone does -- and beyond that steep: M31 at two metres is a dozen
    panels, a project rather than a night (0.27).

    Small: two things go wrong with a small object. It fills little of the
    frame (2% of the field across is a smudge, a fifth or more is framed), and
    it covers few pixels (thirty is a thumbnail, six hundred an image). The
    factor is the geometric mean of the two: at 2000 mm the Ring Nebula is
    small in the frame but 200 pixels across, a crop worth making; at 480 mm
    it is both, and not worth the night.
    """
    panels = np.maximum(np.nan_to_num(fit_long, nan=0.3), 1.0) * np.maximum(
        np.nan_to_num(fit_short, nan=0.3), 1.0
    )
    big = np.where(
        panels <= 2.0,
        panels**-0.35,
        2.0**-0.35 * (np.maximum(panels, 2.0) / 2.0) ** -0.6,
    )
    f = np.where(np.isfinite(fill), fill, 0.3)  # no size: assume a fair fit
    px = np.where(np.isfinite(span_px), span_px, SPAN_PX[1])
    by_fill = np.clip(0.4 + 0.6 * np.log10(np.maximum(f, 1e-6) / 0.02), 0.4, 1.0)
    lo, hi = SPAN_PX
    by_pixels = np.clip(
        0.4 + 0.6 * np.log10(np.maximum(px, 1e-6) / lo) / np.log10(hi / lo), 0.4, 1.0
    )
    small = np.sqrt(by_fill * by_pixels)
    tight = np.where((f > TIGHT_ABOVE) & (panels <= 1.0), 0.92, 1.0)
    return np.asarray(np.maximum(np.minimum(big, small) * tight, 0.15), dtype=np.float64)


def _hours_to_goal(
    sb: NDArray[np.float64],
    altitude_deg: NDArray[np.float64],
    sun_altitude_deg: NDArray[np.float64],
    usable: NDArray[np.bool_] | None = None,
    *,
    site: Site,
    rig: EquipmentPreset,
    snr_goal: float,
    t_sub_s: float,
) -> NDArray[np.float64]:
    """Imaging hours to ``snr_goal`` for each of ``M`` surfaces of brightness
    ``sb`` (M,), priced at the sample where it is quickest.

    ``altitude_deg`` is each object's altitude per sample, (M, S), and
    ``sun_altitude_deg`` the Sun's, (S,) or (M, S). Only samples ``usable``
    marks (M, S; default all) are priced, and each object gets the cheapest
    of them -- the best case the UI labels it as. The highest sample is not
    always the cheapest: an object at its peak in bright twilight can cost
    twice what it does lower in a dark sky. NaN where no usable sample has a
    positive rate.

    The sky is this site's zenith sky, natural plus artificial, plus the
    zenith twilight for that solar altitude, summed as flux, which is how the
    scheduler's sky model adds them (physics/sky.py). Deep in the night the
    twilight term is nothing; in nautical twilight it is 1-2 mag. Moonlight
    and cloud are left out on purpose.

    Hours are wall-clock imaging time: the open-shutter rate is scaled by
    the camera's download duty cycle, ``t_sub / (t_sub + readout)``, exactly
    as plans scale it (pipeline/conditions.py), so the picker and the
    planner quote the same time for the same frames.
    """
    alt = np.asarray(altitude_deg, dtype=np.float64)
    ok = np.broadcast_to(True if usable is None else usable, alt.shape)
    sun = np.broadcast_to(np.asarray(sun_altitude_deg, dtype=np.float64), alt.shape)
    surface = np.broadcast_to(np.asarray(sb, dtype=np.float64)[:, None], alt.shape)
    sky_nl = (
        mag_arcsec2_to_nl(site.natural_zenith_mag_arcsec2)
        + site.artificial_zenith_nl
        + twilight_sky_nl(sun[ok], 90.0)
    )
    rho = np.zeros(alt.shape)
    rho[ok] = extended_snr2_rate(
        surface_brightness=surface[ok],
        airmass=airmass_kasten_young(alt[ok]),
        sky_mag_arcsec2=nl_to_mag_arcsec2(sky_nl),
        optics=rig.optics,
        camera=rig.camera,
        t_sub_s=t_sub_s,
        extinction_k=site.extinction_k,
        element=resolution_element(rig.optics, rig.camera),
    )
    best = rho.max(axis=1, initial=0.0) * download_duty_cycle(t_sub_s, rig.camera.readout_s)
    with np.errstate(divide="ignore"):
        hours = np.where(best > 0, snr_goal * snr_goal / best / 3600.0, np.nan)
    return np.asarray(hours, dtype=np.float64)


def _reach(usable_h: NDArray[np.float64], goal_h: NDArray[np.float64]) -> NDArray[np.float64]:
    """1 when the SNR goal fits in tonight's hours, falling by half for every
    factor of ten it does not, with a floor. A soft preference, not a veto:
    a big faint nebula that needs two nights is still a fine suggestion for
    a small refractor, just not ahead of one it can finish tonight."""
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(goal_h > 0, usable_h / goal_h, 0.0)
    r = 1.0 + 0.5 * np.log10(np.clip(ratio, 1e-3, 1.0))
    return np.asarray(np.clip(r, 0.2, 1.0), dtype=np.float64)


def _recommend(
    st: _Static,
    score: NDArray[np.float64],
    eligible: NDArray[np.bool_],
    feasible: NDArray[np.bool_],
    same_field_arcmin: float,
) -> tuple[list[int], dict[int, list[int]]]:
    """Greedy, varied picks: best score first, but each pick of a family
    (galaxies, nebulae, planetaries, clusters) discounts the next of that
    family, no family fills more than FAMILY_CAP places, and an object sharing
    the field of one already chosen is folded into it rather than listed
    twice. Returns the picks and, per pick, the objects folded into it."""
    order = np.argsort(-np.where(eligible, score, -1.0), kind="stable")
    pool = [int(i) for i in order[: 40 * N_RECOMMENDED] if eligible[i]]
    cos_field = math.cos(math.radians(same_field_arcmin / 60.0))
    picks: list[int] = []
    folded: dict[int, list[int]] = {}
    per_family = np.zeros(5, dtype=np.int64)
    remaining = list(pool)
    while remaining and len(picks) < N_RECOMMENDED:
        adjusted = [
            score[i]
            * (FAMILY_DECAY ** per_family[st.family[i]])
            * (1.0 if feasible[i] else INFEASIBLE_FACTOR)
            * (per_family[st.family[i]] < FAMILY_CAP)
            for i in remaining
        ]
        if max(adjusted) <= 0.0:
            break
        best = int(np.argmax(adjusted))
        i = remaining.pop(best)
        host = next((p for p in picks if float(st.unit[p] @ st.unit[i]) > cos_field), None)
        if host is not None:
            folded.setdefault(host, []).append(i)
            continue
        picks.append(i)
        per_family[st.family[i]] += 1
    return picks, folded


# --------------------------------------------------------------------------
# words
# --------------------------------------------------------------------------


def _duration(hours: float) -> str:
    if hours < 1.0 / 60.0:
        return "under a minute"
    if hours < 1.0:
        return f"{round(hours * 60)} min"
    return f"{hours:.1f} h"


def _framing_phrase(kind: str, fill: float, fit: float) -> str:
    pct = round(fill * 100)
    if kind == "mosaic":
        return f"{fit:.1f}x the frame -- needs a mosaic"
    if kind == "tight":
        return f"spans {pct}% of the frame -- a tight fit"
    if kind == "small":
        return f"only {pct}% of the frame across"
    return f"spans {pct}% of the frame"


def _short(o: DeepSkyObject) -> str:
    return o.designation.split(" · ")[0]


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------


def tonight_out(
    req: schemas.TonightRequest, site_preset: SitePreset, site: Site, rig: EquipmentPreset
) -> schemas.TonightOut:
    try:
        start, end = _window(req, site)
    except ValueError as exc:
        raise ValueError(f"bad date {req.date!r}: {exc}") from exc

    sv, alt, sep = _survey(
        round(site.latitude_deg, 5),
        round(site.longitude_deg, 5),
        round(site.elevation_m, 1),
        start.timestamp(),
        end.timestamp(),
    )
    st = _static()
    n_obj, n_s = alt.shape
    # Each sample stands for step_h hours, and n_s of them never outlast the
    # window -- so no hour count below can exceed it.
    step_h = _sample_seconds(start.timestamp(), end.timestamp()) / 3600.0
    times = [datetime.fromtimestamp(float(t), UTC) for t in sv.times_unix]
    floor = site.min_altitude_deg

    # --- is it up? ---------------------------------------------------------
    dark = sv.sun_altitude_deg <= DARK_SUN_ALT_DEG  # (S,)
    moon_up = sv.moon_altitude_deg > 0.0
    above = alt >= floor  # (N, S)
    moon_ok = ~((sep < site.min_moon_separation_deg) & moon_up[None, :])
    usable = above & moon_ok & dark[None, :] & st.plannable[:, None]
    usable_hours = np.asarray(usable.sum(axis=1) * step_h, dtype=np.float64)
    visible = usable_hours > 0
    # The ranking's dark (nautical) sets the score's scale; the dark reported
    # is astronomical, as /api/night-window counts it -- unless the window
    # never gets there, and then the nautical hours the ranking used, with a
    # note saying so.
    usable_dark_hours = float(dark.sum() * step_h)
    astro = sv.sun_altitude_deg <= ASTRO_DARK_SUN_ALT_DEG
    twilight_only = bool(dark.any() and not astro.any())
    dark_hours = usable_dark_hours if twilight_only else float(astro.sum() * step_h)

    rows_idx = np.arange(n_obj)
    max_alt_dark = (
        np.where(dark[None, :], alt, -90.0).max(axis=1) if dark.any() else alt.max(axis=1)
    )
    best = np.argmax(np.where(usable, alt, -np.inf), axis=1)
    best_alt = alt[rows_idx, best]
    peak = np.argmax(alt, axis=1)
    first = np.argmax(usable, axis=1)
    last = n_s - 1 - np.argmax(usable[:, ::-1], axis=1)

    # --- can I finish? -----------------------------------------------------
    # At each object's cheapest usable moment: its altitude then, and the
    # twilight then. Usually the highest; not when that is in twilight.
    goal = req.snr_goal
    hours_to_goal = np.full(n_obj, np.nan)
    if visible.any():
        hours_to_goal[visible] = _hours_to_goal(
            st.sb[visible],
            alt[visible],
            sv.sun_altitude_deg,
            usable[visible],
            site=site,
            rig=rig,
            snr_goal=goal,
            t_sub_s=req.t_sub_s,
        )
    has_goal = np.isfinite(hours_to_goal)
    feasible = has_goal & (hours_to_goal <= usable_hours)

    # --- does it fit? ------------------------------------------------------
    fov_w, fov_h = rig.fov_deg
    fov_long, fov_short = max(fov_w, fov_h) * 60.0, min(fov_w, fov_h) * 60.0
    fill, fit_long, fit_short, kind = _framing(st, fov_long, fov_short)
    fit = np.fmax(fit_long, fit_short)
    span_px = st.major * 60.0 / rig.pixel_scale_arcsec

    # --- score ---------------------------------------------------------------
    score = np.zeros(n_obj)
    if usable_dark_hours > 0:
        # Six usable hours is a full night's imaging on one target; more is
        # not better, so circumpolar objects do not win on that alone.
        up = np.minimum(usable_hours / min(usable_dark_hours, FULL_CREDIT_HOURS), 1.0)
        height = 0.6 + 0.4 * np.clip((max_alt_dark - floor) / 30.0, 0.0, 1.0)
        reach = np.where(has_goal, _reach(usable_hours, np.nan_to_num(hours_to_goal)), 0.0)
        score = (
            np.sqrt(up)
            * height
            * reach
            * _framing_factor(fit_long, fit_short, fill, span_px)
            * st.appeal
        )
        score = np.where(visible, score, 0.0)

    min_hours = min(MIN_RECOMMEND_HOURS, 0.5 * usable_dark_hours)
    eligible = visible & (usable_hours >= min_hours) & (score > 0)
    picks, folded = _recommend(st, score, eligible, feasible, same_field_arcmin=0.5 * fov_short)

    # --- rows ----------------------------------------------------------------
    # ``times`` are sample MIDPOINTS; a usable span runs from the start of its
    # first sample to the end of its last, and never past the window. bestAt
    # is a usable sample's midpoint, so it lies inside the span.
    half = timedelta(hours=step_h / 2)
    moon_blocked = above & dark[None, :] & ~moon_ok
    rows: list[schemas.TargetSuggestionOut] = []
    for i, o in enumerate(st.objs):
        vis = bool(visible[i])
        # One value for the field and the words, so a row reading 52° never
        # sits over a reason saying 51°: the UI rounds the field half up.
        best_deg = round(float(best_alt[i]), 1)
        f = float(fill[i])
        sized = math.isfinite(f)
        why_not: str | None = None
        reasons: list[str] = []
        if not vis:
            if not o.plannable:
                why_not = o.why_not
            elif float(alt[i].max()) < 0.0:
                why_not = "never rises from here tonight"
            elif not above[i].any():
                why_not = f"peaks at {float(alt[i].max()):.0f}°, below the {floor:.0f}° floor"
            elif not (above[i] & dark).any():
                why_not = f"above {floor:.0f}° only while the sky is light"
            elif moon_blocked[i].any():
                why_not = "too close to the Moon whenever it is high enough"
            else:
                why_not = "not usable tonight"
        else:
            b = int(best[i])
            reasons.append(f"{usable_hours[i]:.1f} h above {floor:.0f}°")
            reasons.append(f"highest {math.floor(best_deg + 0.5)}° at {times[b]:%H:%M} UTC")
            if has_goal[i]:
                reasons.append(f"≈{_duration(float(hours_to_goal[i]))} to SNR {goal:.0f}")
            if sized:
                reasons.append(_framing_phrase(str(kind[i]), f, float(fit[i])))
            if i in folded:
                shares = ", ".join(_short(st.objs[j]) for j in folded[i][:3])
                reasons.append(f"shares the field with {shares}")

        p = int(peak[i])
        b = int(best[i])
        rows.append(
            schemas.TargetSuggestionOut.model_construct(
                id=o.id,
                name=o.name,
                designation=o.designation,
                common_name=o.common_name,
                search_text=o.search_text,
                type=o.type,
                type_label=o.type_label,
                constellation=o.constellation,
                ra_deg=o.ra_deg,
                dec_deg=o.dec_deg,
                ra=st.ra_text[i],
                dec=st.dec_text[i],
                major_arcmin=o.major_arcmin,
                minor_arcmin=o.minor_arcmin,
                v_mag=o.v_mag,
                magnitude=round(o.surface_brightness, 2),
                magnitude_estimated=o.magnitude_estimated,
                messier=o.messier,
                caldwell=o.caldwell,
                plannable=o.plannable,
                visible=vis,
                usable_hours=round(float(usable_hours[i]), 2),
                max_altitude_deg=round(float(max_alt_dark[i]), 1),
                best_altitude_deg=best_deg if vis else None,
                best_at=times[b] if vis else None,
                usable_from=max(times[int(first[i])] - half, start) if vis else None,
                usable_until=min(times[int(last[i])] + half, end) if vis else None,
                transit_at=times[p] if 0 < p < n_s - 1 else None,
                moon_separation_deg=round(float(sep[i, b]), 1) if vis else None,
                why_not=why_not,
                framing=str(kind[i]),
                framing_fill=round(f, 3) if sized else None,
                hours_to_goal=round(float(hours_to_goal[i]), 2) if has_goal[i] else None,
                feasible=bool(feasible[i]),
                score=round(float(score[i]), 4),
                reasons=reasons,
            )
        )

    # Recommendations first, in order; then the rest of the visible sky, best
    # first; then everything else alphabetically, so a search that finds an
    # invisible object finds it in a predictable place.
    rank = {idx: k for k, idx in enumerate(picks)}
    order = sorted(
        range(n_obj),
        key=lambda i: (
            0 if i in rank else 1 if visible[i] else 2,
            rank.get(i, 0),
            -score[i] if visible[i] else 0.0,
            rows[i].name if not visible[i] else "",
        ),
    )
    ordered = [rows[i] for i in order]

    note = (
        "Ranked by hours usable tonight, whether the SNR goal fits in them, altitude, "
        "framing on this camera and how well known the object is, keeping the picks "
        "varied. Exposure estimates assume a clear, moonless sky."
    )
    if twilight_only:
        when = "tonight" if req.start is None else "in this window"
        note += (
            f" The Sun never gets 18° below the horizon {when}, so dark hours here are "
            "nautical twilight and the estimates include the twilight sky."
        )

    return schemas.TonightOut(
        date=req.date,
        site_name=site.name,
        window_start=start,
        window_end=end,
        dark_hours=round(dark_hours, 2),
        moon_illumination=round(float(sv.moon_illumination.max()), 3),
        moon_up_hours=round(float(moon_up.sum() * step_h), 2),
        fov_width_deg=round(fov_w, 4),
        fov_height_deg=round(fov_h, 4),
        catalog_size=n_obj,
        visible_count=int(visible.sum()),
        recommended_ids=[st.objs[i].id for i in picks],
        targets=ordered,
        attribution=CATALOG_ATTRIBUTION,
        ranking_note=note,
    )


def _ra(ra_deg: float) -> str:
    tenths = round((ra_deg % 360.0) / 15.0 * 36000.0)  # tenths of a second of time
    hh, rest = divmod(tenths, 36000)
    mm, ss = divmod(rest, 600)
    return f"{hh % 24:02d}h {mm:02d}m {ss / 10:04.1f}s"


def _dec(dec_deg: float) -> str:
    sign = "+" if dec_deg >= 0 else "-"
    tenths = round(abs(dec_deg) * 36000.0)  # tenths of an arcsecond
    dd, rest = divmod(tenths, 36000)
    am, asec = divmod(rest, 600)
    return f"{sign}{dd:02d}° {am:02d}' {asec / 10:04.1f}\""
