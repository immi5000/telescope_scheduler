"""Domain objects to wire types.

Everything the UI displays is computed here. Nothing downstream is expected to
derive an altitude, an airmass or a field-of-view corner for itself -- see the
module docstring in ``schemas.py`` for why that rule is not negotiable.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray

from tscheduler.api import amend, equipment, outlook, presets, schemas
from tscheduler.api.equipment import EquipmentPreset
from tscheduler.api.live import live_out
from tscheduler.api.presets import SitePreset
from tscheduler.api.session import DecisionPoint, NightSession
from tscheduler.core.hashing import hash_arrays
from tscheduler.domain.plan import SlotKind
from tscheduler.physics.convert import mag_arcsec2_to_nl, nl_to_mag_arcsec2
from tscheduler.physics.geometry import NightGeometry
from tscheduler.pipeline.conditions import PREFERENCE_FACTORS, SKY_COMPONENTS
from tscheduler.scheduling.cpsat import listed_ref_seconds

COMPASS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)

#: Sun altitude boundaries, degrees. -0.833 is the refracted solar upper limb.
TWILIGHT_EDGES: tuple[tuple[str, float], ...] = (
    ("day", -0.833),
    ("civil", -6.0),
    ("nautical", -12.0),
    ("astronomical", -18.0),
)


def compass_point(azimuth_deg: float) -> str:
    return COMPASS[int((azimuth_deg % 360.0) / 22.5 + 0.5) % 16]


def ra_sexagesimal(ra_deg: float) -> str:
    h = ra_deg / 15.0
    hh = int(h)
    mm = int((h - hh) * 60)
    ss = (h - hh - mm / 60.0) * 3600.0
    return f"{hh:02d}h {mm:02d}m {ss:04.1f}s"


def dec_sexagesimal(dec_deg: float) -> str:
    sign = "+" if dec_deg >= 0 else "-"
    d = abs(dec_deg)
    dd = int(d)
    am = int((d - dd) * 60)
    asec = (d - dd - am / 60.0) * 3600.0
    return f"{sign}{dd:02d}° {am:02d}' {asec:04.1f}\""


def _f(x: object, nd: int = 4) -> float:
    """Round, and turn inf/NaN into something JSON can actually carry.

    ``airmass`` is deliberately ``inf`` below the horizon rather than NaN, which
    is right for numpy and fatal for JSON -- ``json.dumps`` emits a bare
    ``Infinity`` that no standards-compliant parser will read back.
    """
    v = float(x)  # type: ignore[arg-type]
    if not math.isfinite(v):
        return 99.0 if v > 0 else -99.0
    return round(v, nd)


def _fl(a: NDArray[np.float64], nd: int = 4) -> list[float]:
    return [_f(v, nd) for v in a]


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------


def site_out(p: SitePreset) -> schemas.SitePresetOut:
    site = p.to_site()
    # Natural and artificial are summed in nanoLamberts and converted ONCE.
    # Adding them as magnitudes is the classic bug in this domain and it is
    # wrong by a few tenths of a mag exactly where it matters most.
    total_nl = mag_arcsec2_to_nl(site.natural_zenith_mag_arcsec2) + site.artificial_zenith_nl
    return schemas.SitePresetOut(
        id=p.id,
        name=p.name,
        latitude_deg=p.latitude_deg,
        longitude_deg=p.longitude_deg,
        elevation_m=p.elevation_m,
        bortle=p.bortle,
        extinction_k=p.extinction_k,
        artificial_zenith_nl=_f(site.artificial_zenith_nl, 2),
        zenith_sky_mag_arcsec2=_f(nl_to_mag_arcsec2(total_nl), 2),
        min_altitude_deg=p.min_altitude_deg,
        min_moon_separation_deg=p.min_moon_separation_deg,
    )


def equipment_out(p: EquipmentPreset) -> schemas.EquipmentOut:
    """Lives with the presets in ``api/equipment.py``; kept here by name because
    this module is where every other wire mapping is looked for."""
    return equipment.equipment_out(p)


# --------------------------------------------------------------------------
# night
# --------------------------------------------------------------------------


def twilight_kind(sun_alt_deg: float) -> str:
    for kind, edge in TWILIGHT_EDGES:
        if sun_alt_deg > edge:
            return kind
    return "night"


def twilight_bands(sess: NightSession, geo: NightGeometry) -> list[schemas.TwilightBandOut]:
    grid = sess.spec.grid
    kinds = [twilight_kind(float(a)) for a in geo.sun_altitude_deg]
    out: list[schemas.TwilightBandOut] = []
    i = 0
    while i < len(kinds):
        j = i
        while j < len(kinds) and kinds[j] == kinds[i]:
            j += 1
        out.append(
            schemas.TwilightBandOut(
                kind=kinds[i],
                from_slot=i,
                to_slot=j,
                starts_at=grid.slot_start(i),
                ends_at=grid.slot_start(j - 1) + (grid.slot_start(1) - grid.slot_start(0)),
            )
        )
        i = j
    return out


def _crossing(values: NDArray[np.float64], rising: bool) -> int | None:
    for i in range(1, len(values)):
        if rising and values[i - 1] <= 0.0 < values[i]:
            return i
        if not rising and values[i - 1] > 0.0 >= values[i]:
            return i
    return None


def moon_out(geo: NightGeometry) -> schemas.MoonOut:
    return schemas.MoonOut(
        altitude_deg=_fl(geo.moon_altitude_deg, 2),
        azimuth_deg=_fl(geo.moon_azimuth_deg, 2),
        illumination=_fl(geo.moon_illumination, 4),
        phase_angle_deg=_fl(geo.moon_phase_angle_deg, 2),
        rise_slot=_crossing(geo.moon_altitude_deg, rising=True),
        set_slot=_crossing(geo.moon_altitude_deg, rising=False),
        max_illumination=_f(np.max(geo.moon_illumination), 4),
    )


def _first_last(mask: NDArray[np.bool_]) -> tuple[int | None, int | None]:
    idx = np.flatnonzero(mask)
    return (int(idx[0]), int(idx[-1])) if idx.size else (None, None)


def targets_out(sess: NightSession) -> list[schemas.TargetOut]:
    geo = sess.geometry
    dps = sess.decision_points
    out: list[schemas.TargetOut] = []
    for i, t in enumerate(sess.spec.targets):
        need_min = 0.0
        # Bounds-checked rather than indexed directly. Targets are appended to
        # a session when an alert is accepted, and the decision points built
        # before that append know nothing about the new row -- so this would
        # raise IndexError on the very next GET, from a line that looks
        # perfectly safe.
        if dps and i < len(dps[0].inp.required_ref_seconds):
            need_min = float(dps[0].inp.required_ref_seconds[i]) / 60.0
        vis = 0
        alt_max = 0.0
        transit: int | None = None
        rise: int | None = None
        sets: int | None = None
        first_usable: int | None = None
        last_usable: int | None = None
        if geo is not None:
            vis = int(geo.visible[i].sum())
            alt_max = float(np.max(geo.altitude_deg[i]))
            transit = int(np.argmax(geo.altitude_deg[i]))
            rise, sets = _first_last(geo.altitude_deg[i] > 0.0)
            first_usable, last_usable = _first_last(geo.visible[i])
        out.append(
            schemas.TargetOut(
                id=t.id,
                name=t.name,
                ra_deg=t.ra_deg,
                dec_deg=t.dec_deg,
                ra=ra_sexagesimal(t.ra_deg),
                dec=dec_sexagesimal(t.dec_deg),
                magnitude=t.magnitude,
                is_point_source=t.is_point_source,
                priority=t.priority,
                urgency=t.urgency,
                snr_goal=t.snr_goal if t.snr_goal is not None else sess.spec.snr_goal,
                required_ref_minutes=_f(min(need_min, 1e6), 1),
                visible_slots=vis,
                max_altitude_deg=_f(alt_max, 2),
                transit_slot=transit,
                rise_slot=rise,
                set_slot=sets,
                first_usable_slot=first_usable,
                last_usable_slot=last_usable,
            )
        )
    return out


def decision_point_out(dp: DecisionPoint) -> schemas.DecisionPointOut:
    return schemas.DecisionPointOut(
        index=dp.index,
        at=dp.at,
        # The fold re-plans at dusk and at each forecast publication; the only
        # other re-plans are the observer's own, adding a target.
        kind="amend"
        if amend.is_amendment(dp.reason)
        else ("start" if dp.index == 0 else "weather"),
        reason=dp.reason,
        plan_id=dp.plan_id,
        records_known=len(dp.plan.ledger.refs),
        newest_published=dp.plan.ledger.max_published,
        changed_slots=dp.changed_slots,
        added_targets=list(dp.added_targets),
        dropped_targets=list(dp.dropped_targets),
    )


def session_out(sess: NightSession) -> schemas.SessionOut:
    grid = sess.spec.grid
    geo = sess.geometry
    empty = np.zeros(grid.n_slots)
    return schemas.SessionOut(
        id=sess.id,
        name=sess.name,
        status=sess.status,
        progress=_f(sess.progress, 3),
        message=sess.message,
        created_at=sess.created_at,
        site=site_out(sess.site_preset),
        equipment=equipment_out(sess.equipment_preset),
        grid=schemas.GridOut(
            start=grid.start,
            end=grid.end,
            slot_minutes=grid.slot_minutes,
            slot_seconds=grid.slot_seconds,
            n_slots=grid.n_slots,
            slot_starts=grid.starts(),
            slot_mids=grid.mids(),
        ),
        targets=targets_out(sess),
        weather_source=sess.weather_source,
        weather=outlook.weather_source_out(sess),
        sun_altitude_deg=_fl(geo.sun_altitude_deg if geo else empty, 2),
        twilight=twilight_bands(sess, geo) if geo else [],
        moon=moon_out(geo)
        if geo
        else schemas.MoonOut(
            altitude_deg=[],
            azimuth_deg=[],
            illumination=[],
            phase_angle_deg=[],
            rise_slot=None,
            set_slot=None,
            max_illumination=0.0,
        ),
        decision_points=[decision_point_out(dp) for dp in sess.decision_points],
        distinct_plans=sess.distinct_plans,
        fold_seconds=_f(sess.fold_seconds, 2),
        error=sess.error,
        live=live_out(sess),
    )


def session_list_item(sess: NightSession) -> schemas.SessionListItemOut:
    return schemas.SessionListItemOut(
        id=sess.id,
        name=sess.name,
        status=sess.status,
        created_at=sess.created_at,
        night_start=sess.spec.grid.start,
        n_targets=len(sess.spec.targets),
        decision_points=len(sess.decision_points),
    )


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def _block_warnings(
    *, alt_min: float, mean_cloud: float, moon_sep_min: float, n_subs: int, min_alt: float
) -> list[schemas.WarningOut]:
    """Warnings carry an action. "Satellite pass expected" tells the observer
    nothing; "discard frames 18-19" tells them what to do at 2 a.m."""
    out: list[schemas.WarningOut] = []
    if n_subs < 9:
        out.append(
            schemas.WarningOut(
                severity="critical",
                category="exposure",
                text=f"only {n_subs} sub-exposures in this block",
                action="shorten the sub or extend the block - sigma-clipping needs 9+ frames "
                "to reject satellite trails and cosmic rays",
            )
        )
    if alt_min < min_alt + 5.0:
        out.append(
            schemas.WarningOut(
                severity="warn",
                category="horizon",
                text=f"drops to {alt_min:.0f}° altitude",
                action="check for trees and roofline before you start; expect softer stars "
                "in the frames nearest the horizon",
            )
        )
    if mean_cloud >= 0.45:
        pct = round(mean_cloud * 100)
        out.append(
            schemas.WarningOut(
                severity="warn" if mean_cloud < 0.7 else "critical",
                category="weather",
                text=f"{pct}% forecast cloud over this block",
                action=f"expect to sigma-clip roughly {pct}% of the frames; keep shooting "
                "rather than stopping - the clip needs the frame count",
            )
        )
    if moon_sep_min < 45.0:
        out.append(
            schemas.WarningOut(
                severity="info",
                category="moon",
                text=f"Moon {moon_sep_min:.0f}° away at the closest",
                action="expect a gradient across the frame; dither and use a gradient "
                "removal step in post",
            )
        )
    return out


def blocks_out(sess: NightSession, dp: DecisionPoint) -> list[schemas.BlockOut]:
    geo = sess.geometry
    assert geo is not None
    grid = sess.spec.grid
    inp = dp.inp
    ids = list(inp.target_ids)
    out: list[schemas.BlockOut] = []

    for blk in dp.plan.blocks:
        i = ids.index(blk.target_id)
        tgt = sess.spec.targets[i]
        a, b = blk.slot_start, blk.slot_end
        data_from = a + blk.switch_slots
        alt = geo.altitude_deg[i][a:b]
        az = geo.azimuth_deg[i][a:b]
        am = geo.airmass[i][a:b]
        sep = geo.moon_separation_deg[i][a:b]
        data = slice(data_from, b)
        eff = inp.eta[i][data]
        pref = inp.preference[i][data]
        cloud = dp.cloud_fraction[data]
        mean_cloud = float(np.mean(cloud)) if cloud.size else 0.0
        goal = tgt.snr_goal if tgt.snr_goal is not None else sess.spec.snr_goal

        out.append(
            schemas.BlockOut(
                target_id=blk.target_id,
                name=tgt.name,
                slot_start=a,
                slot_end=b,
                starts_at=grid.slot_start(a),
                ends_at=grid.slot_start(b),
                integrating_from=grid.slot_start(data_from),
                switch_minutes=_f(blk.switch_slots * grid.slot_minutes, 1),
                integrating_minutes=_f((b - data_from) * grid.slot_minutes, 1),
                ra_deg=tgt.ra_deg,
                dec_deg=tgt.dec_deg,
                ra=ra_sexagesimal(tgt.ra_deg),
                dec=dec_sexagesimal(tgt.dec_deg),
                altitude_start_deg=_f(alt[0], 2),
                altitude_end_deg=_f(alt[-1], 2),
                altitude_min_deg=_f(np.min(alt), 2),
                altitude_max_deg=_f(np.max(alt), 2),
                azimuth_start_deg=_f(az[0], 2),
                azimuth_end_deg=_f(az[-1], 2),
                compass=compass_point(float(az[0])),
                airmass_min=_f(np.min(am), 3),
                airmass_max=_f(np.max(am), 3),
                moon_separation_min_deg=_f(np.min(sep), 2),
                n_subs=blk.n_subs,
                t_sub_s=blk.t_sub_s,
                expected_snr=_f(blk.expected_snr, 2),
                snr_goal=goal,
                mean_efficiency=_f(np.mean(eff) if eff.size else 0.0, 4),
                mean_preference=_f(np.mean(pref) if pref.size else 0.0, 4),
                mean_cloud_fraction=_f(mean_cloud, 4),
                warnings=_block_warnings(
                    alt_min=float(np.min(alt)),
                    mean_cloud=mean_cloud,
                    moon_sep_min=float(np.min(sep)),
                    n_subs=blk.n_subs,
                    min_alt=sess.spec.site.min_altitude_deg,
                ),
            )
        )
    return out


def progress_out(sess: NightSession, dp: DecisionPoint) -> list[schemas.TargetProgressOut]:
    """Accumulated reference-seconds per target, which is the honest measure of
    'how done is it' -- wall minutes on a bad slot are not progress.

    Counted over the frames the blocks list, as their expected SNRs are, so
    the bar and the cards agree: the blocks' SNRs sum in quadrature to this."""
    inp = dp.inp
    listed = listed_ref_seconds(inp, dp.plan.assignments)
    out: list[schemas.TargetProgressOut] = []
    for i, tid in enumerate(inp.target_ids):
        slots = [
            a.slot for a in dp.plan.assignments if a.target_id == tid and a.kind is SlotKind.OBSERVE
        ]
        acc = float(listed[i])
        need = float(inp.required_ref_seconds[i])
        goal = float(inp.snr_goal[i]) if inp.snr_goal is not None else sess.spec.snr_goal
        frac = acc / need if need > 0 else 0.0
        out.append(
            schemas.TargetProgressOut(
                target_id=tid,
                name=sess.spec.targets[i].name,
                included=tid in dp.plan.included,
                accumulated_ref_minutes=_f(acc / 60.0, 2),
                required_ref_minutes=_f(min(need / 60.0, 1e6), 2),
                fraction=_f(min(frac, 9.99), 4),
                expected_snr=_f(goal * math.sqrt(max(frac, 0.0)), 2),
                snr_goal=goal,
                observing_slots=len(slots),
            )
        )
    return out


def _headline(dp: DecisionPoint) -> str:
    if dp.index == 0:
        return "Opening plan for the night."
    bits: list[str] = []
    if dp.added_targets:
        bits.append("added " + ", ".join(dp.added_targets))
    if dp.dropped_targets:
        bits.append("dropped " + ", ".join(dp.dropped_targets))
    if dp.changed_slots:
        bits.append(f"{dp.changed_slots} remaining slot(s) re-assigned")
    if not bits:
        return "New forecast arrived; the plan did not need to change."
    # str.capitalize() would lower-case the rest and turn "UTC" into "utc".
    lead = dp.reason[0].upper() + dp.reason[1:]
    return lead + " — " + "; ".join(bits) + "."


def plan_out(sess: NightSession, index: int) -> schemas.PlanOut:
    dp = sess.decision_points[index]
    valid_from, valid_until = sess.validity(index)
    plan = dp.plan
    led = plan.ledger
    prev_id = sess.decision_points[index - 1].plan_id if index > 0 else None

    return schemas.PlanOut(
        session_id=sess.id,
        plan_id=dp.plan_id,
        decision_index=index,
        decision_count=len(sess.decision_points),
        as_of=dp.at,
        valid_from=valid_from,
        valid_until=valid_until,
        reason=dp.reason,
        status=plan.status,
        objective=_f(plan.objective, 2),
        gap=_f(plan.gap, 4),
        solve_ms=_f(plan.solve_ms, 1),
        summary=plan.summary(),
        blocks=blocks_out(sess, dp),
        slots=[
            schemas.SlotOut(slot=a.slot, kind=a.kind.value, target_id=a.target_id, locked=a.locked)
            for a in plan.assignments
        ],
        dropped=[
            schemas.DroppedOut(
                target_id=d.target_id,
                name=next((t.name for t in sess.spec.targets if t.id == d.target_id), d.target_id),
                code=d.code,
                message=d.message,
            )
            for d in plan.dropped
        ],
        progress=progress_out(sess, dp),
        cloud_fraction=_fl(dp.cloud_fraction, 4),
        seeing_fwhm_arcsec=_fl(dp.seeing_fwhm_arcsec, 3),
        evidence=schemas.EvidenceOut(
            records=len(led.refs),
            sources=sorted({r.provider for r in led.refs}),
            newest_published=led.max_published,
            estimated_fraction=_f(led.estimated_fraction(), 4),
            has_oracle=led.has_oracle,
        ),
        changes=schemas.ChangesOut(
            from_plan_id=prev_id,
            locked_through_slot=dp.locked_through_slot,
            changed_slots=dp.changed_slots,
            added_targets=list(dp.added_targets),
            dropped_targets=list(dp.dropped_targets),
            past_slots_rewritten=dp.past_slots_rewritten,
            headline=_headline(dp),
        ),
        outlook=outlook.outlook_out(sess, dp),
        slot_weather=outlook.slot_weather_out(sess, dp),
    )


# --------------------------------------------------------------------------
# geometry -- the as_of-INDEPENDENT half
# --------------------------------------------------------------------------


def geometry_id(geo: NightGeometry) -> str:
    """Content hash over every geometry array.

    Used as the ETag. It is a hash of the arrays rather than of the session id
    because two sessions at the same site on the same night with the same
    targets genuinely have the same geometry, and should share a cache entry.
    """
    return hash_arrays(geo.arrays())[:16]


def geometry_out(sess: NightSession) -> schemas.GeometryOut:
    geo = sess.geometry
    assert geo is not None
    grid = sess.spec.grid
    return schemas.GeometryOut(
        session_id=sess.id,
        geometry_id=geometry_id(geo),
        n_slots=grid.n_slots,
        slot_mids=grid.mids(),
        lst_hours=_fl(geo.lst_hours, 6),
        # Six decimals is ~0.2 arcsec of orientation. Four would be 20 arcsec,
        # comparable to the aberration floor this rotation already carries, and
        # would show up as a visible per-slot jitter in the horizon.
        frame_quat=[[_f(v, 6) for v in q] for q in geo.frame_quat],
        sun_altitude_deg=_fl(geo.sun_altitude_deg, 3),
        sun_azimuth_deg=_fl(geo.sun_azimuth_deg, 3),
        moon=moon_out(geo),
        rows=[
            schemas.GeometryRowOut(
                target_id=t.id,
                name=t.name,
                ra_deg=t.ra_deg,
                dec_deg=t.dec_deg,
                altitude_deg=_fl(geo.altitude_deg[i], 3),
                azimuth_deg=_fl(geo.azimuth_deg[i], 3),
                airmass=_fl(geo.airmass[i], 3),
                moon_separation_deg=_fl(geo.moon_separation_deg[i], 2),
                visible=[bool(v) for v in geo.visible[i]],
            )
            for i, t in enumerate(sess.spec.targets)
        ],
    )


# --------------------------------------------------------------------------
# quality grid -- the as_of-DEPENDENT half
# --------------------------------------------------------------------------


def quality_grid_out(sess: NightSession, index: int) -> schemas.QualityGridOut:
    dp = sess.decision_points[index]
    cond = dp.conditions
    rows = [
        schemas.GridRowOut(
            target_id=tid,
            name=sess.spec.targets[i].name,
            efficiency=_fl(cond.eta[i], 4),
            preference=_fl(cond.preference[i], 4),
            preference_factors={
                k: _fl(cond.preference_factors[k][i], 4) for k in PREFERENCE_FACTORS
            },
            sky_mag_arcsec2=_fl(cond.sky_mag_arcsec2[i], 3),
            # Two decimals of nanoLamberts: a pristine zenith is ~54 nL and the
            # faintest component worth drawing is ~0.1 nL, so this is three
            # significant figures where it matters and short on the wire.
            sky_components_nl={k: _fl(cond.sky_components_nl[k][i], 2) for k in SKY_COMPONENTS},
        )
        for i, tid in enumerate(dp.inp.target_ids)
    ]
    return schemas.QualityGridOut(
        session_id=sess.id,
        as_of=dp.at,
        plan_id=dp.plan_id,
        n_slots=sess.spec.grid.n_slots,
        rows=rows,
        cloud_fraction=_fl(dp.cloud_fraction, 4),
        seeing_fwhm_arcsec=_fl(dp.seeing_fwhm_arcsec, 3),
    )


def utc(t: datetime) -> datetime:
    return t.astimezone(UTC)


# --------------------------------------------------------------------------
# catalogue, for the sky to draw and pick from
# --------------------------------------------------------------------------


def catalog_out() -> schemas.CatalogOut:
    """Every catalogue object, positioned, so anything in the sky can be picked
    and added to a night. Not ranked and not night-specific: the sky places each
    object with the session's own frame rotation, as it does the targets."""
    return schemas.CatalogOut(
        size=len(presets.CATALOG),
        attribution=presets.CATALOG_ATTRIBUTION,
        objects=[
            schemas.CatalogObjectOut(
                id=o.id,
                name=o.target_name,
                designation=o.designation,
                type=o.type,
                type_label=o.type_label,
                constellation=o.constellation,
                ra_deg=_f(o.ra_deg, 5),
                dec_deg=_f(o.dec_deg, 5),
                ra=ra_sexagesimal(o.ra_deg),
                dec=dec_sexagesimal(o.dec_deg),
                major_arcmin=None if o.major_arcmin is None else _f(o.major_arcmin, 2),
                v_mag=None if o.v_mag is None else _f(o.v_mag, 2),
                magnitude=_f(o.surface_brightness, 2),
                plannable=o.plannable,
                why_not=o.why_not,
            )
            for o in presets.CATALOG
        ],
    )
