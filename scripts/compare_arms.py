#!/usr/bin/env python
"""Naive vs static vs adaptive, on a real past night.

The project's central claim, measured. All three arms see the same night, the
same targets, the same equipment, and are scored against the same realized
conditions -- never against the forecast each planned with.

    uv run python scripts/compare_arms.py --date 2026-09-13
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from tscheduler.core.timegrid import TimeGrid
from tscheduler.domain.equipment import Camera, Mount, Optics
from tscheduler.domain.site import Site
from tscheduler.domain.targets import Target
from tscheduler.evaluation.arms import run_comparison
from tscheduler.physics.convert import bortle_to_artificial_nl
from tscheduler.pipeline.builder import SessionSpec, build_geometry
from tscheduler.providers.weather.model import WeatherQuery
from tscheduler.providers.weather.open_meteo import OpenMeteoReplayForecast

CATALOG = [
    ("m31", "M31", 10.6847, 41.2690, 18.4),
    ("m27", "M27", 299.9015, 22.7211, 18.4),
    ("m57", "M57", 283.3963, 33.0292, 19.0),
    ("n7000", "NGC7000", 314.75, 44.37, 18.0),
    ("m13", "M13", 250.4235, 36.4613, 18.8),
    ("m33", "M33", 23.4621, 30.6602, 18.7),
    ("n7331", "NGC7331", 339.2671, 34.4158, 19.3),
    ("n6946", "NGC6946", 308.718, 60.1539, 19.1),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default="2026-09-13")
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
        f"\n  COMPARISON  {grid.start:%Y-%m-%d %H:%M}-{grid.end:%H:%M} UTC  "
        f"({len(CATALOG)} targets, SNR {args.snr:.0f}, Bortle {args.bortle})"
    )
    print("  fetching real Open-Meteo runs...")
    records = provider._load_all_runs(q)
    pubs = sorted({r.published_at for r in records})
    during = [p for p in pubs if grid.start < p < grid.end]
    print(f"  {len(pubs)} runs, {len(during)} published during the session\n")

    geo = build_geometry(spec)
    report = run_comparison(spec, geo, provider, during)

    keys = [
        ("targets_completed", "targets completed *", "{:.0f}"),
        ("science_value", "science value (partial)", "{:.2f}"),
        ("open_shutter_fraction", "open shutter", "{:.1%}"),
        ("mean_altitude_deg", "mean altitude", "{:.1f} deg"),
        ("mean_efficiency", "mean efficiency", "{:.3f}"),
        ("useful_ref_minutes", "useful ref-min", "{:.0f}"),
    ]
    w = 26
    print("  " + "metric".ljust(w) + "".join(a.name.rjust(13) for a in report.arms))
    print("  " + "-" * (w + 13 * len(report.arms)))
    for key, label, fmt in keys:
        row = "  " + label.ljust(w)
        for a in report.arms:
            row += fmt.format(a.metrics[key]).rjust(13)
        print(row)
    print("  " + "replans".ljust(w) + "".join(str(a.n_replans).rjust(13) for a in report.arms))
    print(
        "  "
        + "plan churn (slots)".ljust(w)
        + "".join(str(a.plan_churn).rjust(13) for a in report.arms)
    )

    naive, static, adaptive = (report.by_name(n) for n in ("naive", "static", "adaptive"))
    print()
    print("  * PRIMARY METRIC is targets completed. An SNR goal is a THRESHOLD:")
    print("    a target at 50% of goal is usually scientifically worthless, so the")
    print("    partial-credit score below it rewards spreading effort thin and")
    print("    should not be read as the headline.")
    print()
    print("  " + "-" * (w + 13 * 3))
    print("  HEADLINE (the claim this project exists to test)")
    d_opt = static.metrics["targets_completed"] - naive.metrics["targets_completed"]
    d_adapt = adaptive.metrics["targets_completed"] - static.metrics["targets_completed"]
    print(f"    optimising at all       : {d_opt:+.0f} targets vs naive")
    print(f"    re-planning on new data : {d_adapt:+.0f} targets vs static")
    if d_adapt <= 0:
        print("    -> adaptive did NOT beat static on this night. Reported as found;")
        print("       a single night is not evidence either way.")
    print()
    print("  Scored against realized conditions, not the forecast each arm planned with.")
    print(
        f"  Truth cloud cover: mean {report.truth_cloud.mean():.0%}, "
        f"max {report.truth_cloud.max():.0%}\n"
    )


if __name__ == "__main__":
    main()
