#!/usr/bin/env python
"""Replay a real past night, showing the plan adapt as forecasts arrive.

Fetches genuine Open-Meteo model runs for a night in the last week, then walks
the timeline of publication instants. At each one it re-plans using ONLY the
data published by that moment, and prints what changed.

    uv run python scripts/replay_night.py --date 2026-09-13

This is the core claim of the project made visible: the plan at 01:00 is not the
plan at 05:00, because at 01:00 the newer forecast did not exist yet.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from tscheduler.core.clock import AsOf
from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.plan import SlotKind
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.physics.convert import bortle_to_artificial_nl
from tscheduler.pipeline.builder import SessionSpec, build_geometry, build_scheduler_input
from tscheduler.providers.weather.model import WeatherQuery
from tscheduler.providers.weather.open_meteo import OpenMeteoReplayForecast
from tscheduler.scheduling.cpsat import SolveOptions, build_and_solve

CATALOG = [
    ("m31", "M31", 10.6847, 41.2690, 18.4),
    ("m27", "M27", 299.9015, 22.7211, 18.4),
    ("m57", "M57", 283.3963, 33.0292, 19.0),
    ("n7000", "NGC7000", 314.75, 44.37, 18.0),
    ("m13", "M13", 250.4235, 36.4613, 18.8),
    ("m33", "M33", 23.4621, 30.6602, 18.7),
    ("n7331", "NGC7331", 339.2671, 34.4158, 19.3),
]

# Replay MUST be deterministic: CP-SAT with a wall-clock limit and workers > 1
# returns different equal-value optima run to run, which would show up as
# phantom plan churn and make the whole comparison meaningless.
DET = SolveOptions(deterministic=True, max_deterministic_time=4.0, random_seed=1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default="2026-09-13", help="night start date, UTC")
    ap.add_argument("--lat", type=float, default=40.1164)
    ap.add_argument("--lon", type=float, default=-88.2434)
    ap.add_argument("--bortle", type=int, default=5)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--snr", type=float, default=60.0)
    args = ap.parse_args()

    d = datetime.strptime(args.date, "%Y-%m-%d").replace(hour=1, tzinfo=UTC)
    grid = TimeGrid.from_window(d, d + timedelta(hours=args.hours))
    spec = SessionSpec(
        site=Site(
            latitude_deg=args.lat,
            longitude_deg=args.lon,
            elevation_m=227.0,
            artificial_zenith_nl=bortle_to_artificial_nl(args.bortle),
        ),
        grid=grid,
        optics=Optics(aperture_mm=203.0, focal_length_mm=2032.0, central_obstruction_mm=68.0),
        camera=Camera(
            pixel_size_um=3.76,
            sensor_width_px=6248,
            sensor_height_px=4176,
            quantum_efficiency=0.75,
            read_noise_e=1.5,
            dark_current_e_per_s=0.002,
        ),
        mount=Mount(),
        targets=tuple(Target(i, n, ra, dec, m) for i, n, ra, dec, m in CATALOG),
        snr_goal=args.snr,
    )

    provider = OpenMeteoReplayForecast()
    q = WeatherQuery(lat=args.lat, lon=args.lon, valid_from=grid.start, valid_to=grid.end)

    print(
        f"\n  REPLAY  {grid.start:%Y-%m-%d %H:%M} - {grid.end:%H:%M} UTC  "
        f"({args.lat:+.3f},{args.lon:+.3f}, Bortle {args.bortle}, SNR {args.snr:.0f})"
    )
    print("  fetching real Open-Meteo model runs...")
    records = provider._load_all_runs(q)
    pubs = sorted({r.published_at for r in records})
    print(
        f"  {len(records)} records from {len(pubs)} runs, "
        f"published {pubs[0]:%m-%d %H:%M} .. {pubs[-1]:%m-%d %H:%M} UTC"
    )

    geo = build_geometry(spec)

    # Decision points: session start, plus every run published DURING the night.
    during = [p for p in pubs if grid.start < p < grid.end]
    instants = [grid.start, *during]
    print(f"  {len(instants)} decision point(s) inside the session\n")
    print("  " + "-" * 76)

    prev_plan = None
    prev_ids: frozenset[str] = frozenset()
    for t in instants:
        # A fold, not a sequence of independent point queries. Slots already
        # past at this as_of are LOCKED to whatever the accepted plan had them
        # doing -- those photons were either collected or missed, and no
        # re-plan can change that. Without this the "replay" would rewrite
        # history every time a forecast arrived, which is both wrong and the
        # easiest way to accidentally manufacture a good-looking result.
        first_free = grid.index_of(t) if t > grid.start else 0
        locked: dict[int, str | None] = {}
        locked_observing: frozenset[int] = frozenset()
        previous: dict[int, str | None] = {}
        if prev_plan is not None:
            locked = {s: prev_plan.assignments[s].target_id for s in range(first_free)}
            locked_observing = frozenset(
                s for s in range(first_free) if prev_plan.assignments[s].kind is SlotKind.OBSERVE
            )
            previous = {
                s: prev_plan.assignments[s].target_id for s in range(first_free, grid.n_slots)
            }

        inp, ledger = build_scheduler_input(
            spec,
            geo,
            provider,
            AsOf.at(t),
            locked=locked,
            locked_observing=locked_observing,
            previous_plan=previous,
            first_free_slot=first_free,
        )
        plan = build_and_solve(inp, AsOf.at(t), DET, ledger)
        cloud, _, _ = provider.series(q, AsOf.at(t), grid)

        newest = ledger.max_published
        why = "session start" if t == grid.start else f"new {newest:%H:%M} run arrived"
        print(f"  as_of {t:%H:%M}  ({why})")
        print(
            f"    knows {len(ledger.refs)} records, newest published "
            f"{newest:%m-%d %H:%M} | forecast mean cloud {cloud.mean():.0%}"
        )

        order = [b.target_id for b in plan.blocks]
        print(
            f"    plan: {' -> '.join(order) if order else '(nothing schedulable)'}"
            f"   [{plan.status}]"
        )
        if not plan.assignments:
            print("    !! solver returned no assignments -- this is a bug, not a data condition")
            continue

        if prev_plan is not None:
            gained = sorted(plan.included - prev_ids)
            lost = sorted(prev_ids - plan.included)
            changed = sum(
                1
                for i in range(first_free, grid.n_slots)
                if plan.assignments[i].target_id != prev_plan.assignments[i].target_id
            )
            rewritten = sum(
                1
                for i in range(first_free)
                if plan.assignments[i].target_id != prev_plan.assignments[i].target_id
            )
            assert rewritten == 0, f"{rewritten} PAST slots were rewritten -- locking failed"
            print(f"    locked: slots 0-{first_free - 1} are history and cannot move")
            if not gained and not lost and changed == 0:
                print("    unchanged")
            else:
                bits = []
                if gained:
                    bits.append(f"added {', '.join(gained)}")
                if lost:
                    bits.append(f"dropped {', '.join(lost)}")
                bits.append(f"{changed} slot(s) differ")
                print(f"    CHANGED: {'; '.join(bits)}")
        print()
        prev_plan, prev_ids = plan, plan.included

    print("  " + "-" * 76)
    print("  Every plan above used only data published at or before its own as_of.")
    print("  Past slots are never rewritten; only the remaining night is re-planned.\n")


if __name__ == "__main__":
    main()
