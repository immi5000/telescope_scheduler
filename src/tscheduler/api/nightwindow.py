"""Dusk and dawn for a site and a date.

Exists so the setup form can default to "astronomical dusk until dawn" without
the browser computing it. The rule that every displayed number is computed
server-side is not about tidiness: the moment the client derives a twilight
time it will disagree with the one the scheduler used, by a couple of minutes,
and someone will spend an evening on a form that says 20:41 while the timeline
starts at 20:43.

Cheap: one astropy call over ~36 hours of four-minute samples, which is a few
milliseconds -- the sun is one body, not a target grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, get_body
from astropy.time import Time

from tscheduler.physics.geometry import configure_astropy_offline

#: Refracted solar upper limb, and the astronomical twilight boundary.
SUNSET_ALT_DEG = -0.833
DARK_ALT_DEG = -18.0

_SAMPLE_MINUTES = 4


@dataclass(frozen=True, slots=True)
class NightWindow:
    sunset: datetime | None
    dusk: datetime | None
    dawn: datetime | None
    sunrise: datetime | None
    dark_hours: float
    utc_offset_hours: float
    """Mean SOLAR offset from longitude, not a civil timezone.

    Deliberately not a civil offset: deriving one needs a tz-boundary database
    and a DST table, and both are wrong at exactly the moments people care
    about. Solar offset is what actually governs when it gets dark, it is
    exact, and it is labelled as such in the UI so nobody mistakes it for
    wall-clock time.
    """
    always_up: bool
    """No sunset at all -- a polar summer. The caller must not treat a missing
    dusk as an error."""
    never_rises: bool


def _crossing(times: np.ndarray, alt: np.ndarray, level: float, falling: bool) -> datetime | None:
    """First crossing of ``level``, linearly interpolated between samples."""
    above = alt > level
    for i in range(1, len(alt)):
        crossed = (above[i - 1] and not above[i]) if falling else (not above[i - 1] and above[i])
        if not crossed:
            continue
        a, b = alt[i - 1], alt[i]
        f = 0.0 if a == b else (a - level) / (a - b)
        t = times[i - 1] + f * (times[i] - times[i - 1])
        return datetime.fromtimestamp(float(t), UTC)
    return None


def night_window(
    latitude_deg: float,
    longitude_deg: float,
    elevation_m: float,
    date: str,
) -> NightWindow:
    """Twilight boundaries for the night that BEGINS on ``date`` at this site.

    Anchored on local solar noon rather than on midnight UTC, because "the
    night of the 13th" means the one that starts on the evening of the 13th,
    and at longitude -155 that night is mostly the 14th in UTC. Getting this
    wrong shifts every default by a day for anyone west of Greenwich.
    """
    configure_astropy_offline()
    # 271.76 E is 88.24 W: without the wrap the solar offset is +18 h and
    # "the night of" lands on the previous evening.
    longitude_deg = ((longitude_deg + 180.0) % 360.0) - 180.0
    offset_hours = longitude_deg / 15.0

    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    local_noon = day + timedelta(hours=12 - offset_hours)

    n = int(36 * 60 / _SAMPLE_MINUTES) + 1
    unix = local_noon.timestamp() + np.arange(n) * _SAMPLE_MINUTES * 60.0
    times = Time(unix, format="unix", scale="utc")
    location = EarthLocation(
        lat=latitude_deg * u.deg, lon=longitude_deg * u.deg, height=elevation_m * u.m
    )
    alt = np.asarray(
        get_body("sun", times, location)
        .transform_to(AltAz(obstime=times, location=location))
        .alt.deg,
        dtype=np.float64,
    )

    sunset = _crossing(unix, alt, SUNSET_ALT_DEG, falling=True)
    dusk = _crossing(unix, alt, DARK_ALT_DEG, falling=True)
    after = unix > (dusk.timestamp() if dusk else (sunset.timestamp() if sunset else unix[0]))
    dawn = _crossing(unix[after], alt[after], DARK_ALT_DEG, falling=False)
    sunrise = _crossing(unix[after], alt[after], SUNSET_ALT_DEG, falling=False)

    dark = 0.0
    if dusk and dawn and dawn > dusk:
        dark = (dawn - dusk).total_seconds() / 3600.0
    elif sunset and sunrise and sunrise > sunset:
        # Never fully dark: a high-latitude summer, or a short northern night.
        # Report the sun-down span so the form still offers a usable window
        # rather than collapsing to zero hours.
        dark = (sunrise - sunset).total_seconds() / 3600.0

    return NightWindow(
        sunset=sunset,
        dusk=dusk,
        dawn=dawn,
        sunrise=sunrise,
        dark_hours=round(dark, 3),
        utc_offset_hours=round(offset_hours, 3),
        always_up=bool(np.all(alt > SUNSET_ALT_DEG)),
        never_rises=bool(np.all(alt < SUNSET_ALT_DEG)),
    )
