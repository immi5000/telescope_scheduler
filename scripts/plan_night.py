#!/usr/bin/env python
"""Plan a night and print instruction cards.

Offline demo: uses a synthetic forecast so it runs with no network and no
credentials. Real providers arrive at milestone 7; the point of this script is
that swapping FixtureForecast for a live provider is the ONLY change needed,
which is what the provider seam is for.

    uv run python scripts/plan_night.py --bortle 5 --snr 60
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

import numpy as np

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.convert import bortle_to_artificial_nl
from tscheduler.pipeline.builder import SessionSpec, build_geometry, build_scheduler_input
from tscheduler.providers.weather.fixture import FixtureForecast
from tscheduler.scheduling.cpsat import SolveOptions, build_and_solve

CATALOG = [
    ("m31", "M31 Andromeda", 10.6847, 41.2690, 18.4),
    ("m27", "M27 Dumbbell", 299.9015, 22.7211, 18.4),
    ("m57", "M57 Ring", 283.3963, 33.0292, 19.0),
    ("n7000", "NGC7000 N.America", 314.75, 44.37, 18.0),
    ("m13", "M13 Hercules", 250.4235, 36.4613, 18.8),
    ("n6946", "NGC6946 Fireworks", 308.718, 60.1539, 19.1),
    ("m33", "M33 Triangulum", 23.4621, 30.6602, 18.7),
    ("n7331", "NGC7331", 339.2671, 34.4158, 19.3),
]


def sexagesimal(ra_deg: float, dec_deg: float) -> tuple[str, str]:
    h = ra_deg / 15.0
    hh = int(h)
    mm = int((h - hh) * 60)
    ss = (h - hh - mm / 60) * 3600
    sign = "+" if dec_deg >= 0 else "-"
    d = abs(dec_deg)
    dd = int(d)
    am = int((d - dd) * 60)
    asec = (d - dd - am / 60) * 3600
    return f"{hh:02d}h{mm:02d}m{ss:04.1f}s", f"{sign}{dd:02d}d{am:02d}'{asec:04.1f}\""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lat", type=float, default=40.1164)
    p.add_argument("--lon", type=float, default=-88.2434, help="positive EAST")
    p.add_argument("--elev", type=float, default=227.0)
    p.add_argument("--bortle", type=int, default=5, choices=range(1, 10))
    p.add_argument("--aperture", type=float, default=203.0, help="mm")
    p.add_argument("--focal", type=float, default=2032.0, help="mm")
    p.add_argument("--snr", type=float, default=60.0)
    p.add_argument("--hours", type=float, default=8.0)
    p.add_argument("--switch", type=float, default=5.0, help="minutes")
    p.add_argument("--cloud-at", type=float, default=0.6, help="fraction of night before cloud")
    args = p.parse_args()

    site = Site(
        latitude_deg=args.lat,
        longitude_deg=args.lon,
        elevation_m=args.elev,
        artificial_zenith_nl=bortle_to_artificial_nl(args.bortle),
    )
    spec = SessionSpec(
        site=site,
        grid=TimeGrid.from_window(
            datetime(2026, 9, 13, 1, 0, tzinfo=UTC),
            datetime(2026, 9, 13, 1, 0, tzinfo=UTC) + timedelta(hours=args.hours),
        ),
        optics=Optics(
            aperture_mm=args.aperture, focal_length_mm=args.focal, central_obstruction_mm=68.0
        ),
        camera=Camera(
            pixel_size_um=3.76,
            sensor_width_px=6248,
            sensor_height_px=4176,
            quantum_efficiency=0.75,
            read_noise_e=1.5,
            dark_current_e_per_s=0.002,
        ),
        mount=Mount(switch_minutes=args.switch),
        targets=tuple(Target(i, n, ra, dec, m) for i, n, ra, dec, m in CATALOG),
        snr_goal=args.snr,
    )

    grid = spec.grid
    n_hours = int(args.hours) + 2
    turn = int(n_hours * args.cloud_at)
    forecast = FixtureForecast.from_runs(
        [
            (
                grid.start - timedelta(hours=6),
                [
                    (grid.start + timedelta(hours=h), 0.10 if h < turn else 0.80)
                    for h in range(n_hours)
                ],
            )
        ]
    )

    geo = build_geometry(spec)
    inp, ledger = build_scheduler_input(spec, geo, forecast, AsOf.at(grid.start))
    plan = build_and_solve(inp, AsOf.at(grid.start), SolveOptions(max_seconds=5.0), ledger)

    print(f"\n  NIGHT PLAN  {grid.start:%Y-%m-%d %H:%M} - {grid.end:%H:%M} UTC")
    print(
        f"  Site {args.lat:+.4f},{args.lon:+.4f} Bortle {args.bortle} | "
        f"{args.aperture:.0f}mm f/{args.focal / args.aperture:.1f} | SNR goal {args.snr:.0f}"
    )
    print(f"  {plan.summary()}\n")
    print("  " + "-" * 74)

    for blk in plan.blocks:
        i = inp.target_ids.index(blk.target_id)
        tgt = spec.targets[i]
        a, b = grid.slot_start(blk.slot_start), grid.slot_start(blk.slot_end)
        ra_s, dec_s = sexagesimal(tgt.ra_deg, tgt.dec_deg)
        alt = geo.altitude_deg[i][blk.slot_start : blk.slot_end]
        data_slots = blk.n_slots - blk.switch_slots
        mins = data_slots * grid.slot_minutes

        if blk.switch_slots:
            sw_a = grid.slot_start(blk.slot_start)
            sw_b = grid.slot_start(blk.slot_start + blk.switch_slots)
            print(f"  {sw_a:%H:%M}-{sw_b:%H:%M}  move to {tgt.name} and focus")
        print(f"  {a:%H:%M}-{b:%H:%M} | {tgt.name}")
        print(f"    RA {ra_s}  Dec {dec_s}   altitude {alt.min():.0f}->{alt.max():.0f} deg")
        print(f"    {blk.n_subs} x {blk.t_sub_s:.0f}s  ({mins} min integrating)")
        cl = inp.preference[i][blk.slot_start : blk.slot_end]
        if float(np.min(alt)) < 35:
            print("    ! low altitude for part of this block")
        if float(np.mean(cl)) < 0.4:
            print("    ! marginal conditions - expect to discard frames")
        print()

    if plan.dropped:
        print("  " + "-" * 74)
        print("  NOT SCHEDULED")
        for d in plan.dropped:
            i = inp.target_ids.index(d.target_id)
            vis = int(inp.visible[i].sum())
            need_min = inp.required_ref_seconds[i] / 60
            # Reference-minutes are not wall minutes: a slot at eta=0.3 buys 0.3
            # of a reference minute. Comparing need against raw visible minutes
            # would claim "not enough sky" for targets that actually lost to
            # contention, which is a different and much more useful answer.
            available_ref_min = float(inp.eta[i].sum()) * grid.slot_minutes
            if vis == 0:
                why = "never above the altitude limit tonight"
            elif need_min > 1e6:
                why = "cannot reach the SNR goal at all with this equipment"
            elif need_min > available_ref_min:
                why = (
                    f"needs {need_min:.0f} reference-min, but its visible window "
                    f"only offers {available_ref_min:.0f} at tonight's sky brightness"
                )
            else:
                why = (
                    f"fits in principle ({need_min:.0f} of {available_ref_min:.0f} "
                    f"reference-min available) but was outranked for the slots it needed"
                )
            print(f"    {spec.targets[i].name:22} {why}")
    print()


if __name__ == "__main__":
    main()
