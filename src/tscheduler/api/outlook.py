"""Where the weather came from, and what it says about the night.

Three readers, three functions:

``weather_source_out``  the session's weather source -- archive, forecast,
                        synthetic or unavailable -- in one sentence, with what
                        actually happened (a night resolved to "forecast" whose
                        fetch produced nothing is reported as unavailable).
``outlook_out``         a plain-language read of the forecast AS IT STOOD at
                        one decision point. It reads only that point's
                        conditions layer, which the publication gate already
                        bounded by its as_of, so scrubbing back to dusk shows
                        what was knowable at dusk.
``slot_weather_out``    the same forecast, slot by slot, for the weather box
                        under the time cursor: raw values, ``None`` where the
                        source said nothing.

The rules, in one place so the wording and the numbers cannot drift apart:

* **Dark slots** are Sun altitude < -18°. If the Sun never gets that low (a
  high-latitude summer) the summary falls back to < -12°, and failing that to
  the whole session, and a note says which.
* **The rest of the night only.** A plan is summarised from its decision
  point's first free slot on: a plan made at 03:00 is not advised about
  01:00. Once dark hours are behind it the wording says "remaining", and a
  plan made after the last dark slot says the dark hours are over.
* **Coverage.** Only slots a forecast reaches are summarised. Past its reach
  the plan assumes a clear sky, a critical note says from when, and the
  headline and verdict count those hours as not forecast -- never as clear.
* **Clear** is cloud cover < 30%. ``clear_hours`` counts covered dark slots
  under it.
* **Category** is the NWS sky-cover term for the mean dark-hour cover, in
  eighths: < 1/8 clear, < 3/8 mostly clear, < 5/8 partly cloudy, < 7/8 mostly
  cloudy, else overcast.
* **Verdict** go if clear hours >= 60% of the dark hours or >= 4 h; marginal
  if >= 1 h; poor otherwise; unknown without data -- or when the hours the
  forecast does not reach could change it either way.
* **Best window** is the longest contiguous run of clear dark slots. A
  cloudier gap of at most ``BRIDGE_MINUTES`` inside a clear stretch does not
  split it -- hourly forecasts interpolated onto 5-minute slots flicker across
  30% at the edges, and "20:05-22:40 and 22:45-01:30" reads worse than one
  window whose mean cloud already reflects the blip. A window shorter than
  ``MIN_WINDOW_MINUTES`` is not reported.
* **Trend** compares the first and last thirds of the dark slots; a change of
  25 percentage points or more earns a note, timed at the change point that
  best splits the dark hours into a before and an after.
* **Dew** is likely when the dew-point spread drops under 2 °C or humidity
  reaches 95%. **Wind** warns at 8 m/s and is critical at 12 m/s (mean, 10 m);
  gusts, when the model gives them, warn at 12 and are critical at 17 m/s.

Every note says what to DO. Times are "HH:MM UTC", as in the rest of the UI.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import numpy as np
from numpy.typing import NDArray

from tscheduler.api import schemas
from tscheduler.api.session import DecisionPoint, NightSession
from tscheduler.core.timegrid import TimeGrid
from tscheduler.pipeline.conditions import ConditionsLayer
from tscheduler.providers.weather.auto import (
    FetchReport,
    NightTiming,
    WeatherMode,
    fmt_utc,
)
from tscheduler.providers.weather.fixture import FixtureForecast
from tscheduler.providers.weather.open_meteo import (
    LIVE_SOURCE_ID,
    RUNS_SOURCE_ID,
    model_name,
    run_of,
)

CLEAR_BELOW: Final = 0.30
DARK_LIMITS: Final = ((-18.0, "astronomical"), (-12.0, "nautical"))
BRIDGE_MINUTES: Final = 10
MIN_WINDOW_MINUTES: Final = 15
TREND_DELTA: Final = 0.25
DEW_SPREAD_C: Final = 2.0
DEW_HUMIDITY: Final = 0.95
WIND_WARN_MS: Final = 8.0
WIND_CRIT_MS: Final = 12.0
GUST_WARN_MS: Final = 12.0
GUST_CRIT_MS: Final = 17.0
GO_FRACTION: Final = 0.6
GO_HOURS: Final = 4.0
MARGINAL_HOURS: Final = 1.0

#: (upper bound on mean cover, key, label) -- the NWS sky-cover terms by oktas.
CATEGORIES: Final = (
    (1 / 8, "clear", "Clear"),
    (3 / 8, "mostly-clear", "Mostly clear"),
    (5 / 8, "partly-cloudy", "Partly cloudy"),
    (7 / 8, "mostly-cloudy", "Mostly cloudy"),
)

SOURCE_LABELS: Final[dict[WeatherMode, str]] = {
    WeatherMode.ARCHIVE: "Archived forecasts",
    WeatherMode.FORECAST: "Live forecast",
    WeatherMode.SYNTHETIC: "Synthetic demo weather",
    WeatherMode.UNAVAILABLE: "No forecast available",
}

_SEVERITY_ORDER: Final = {"critical": 0, "warn": 1, "info": 2}

#: What the summarised slots are called, by the darkness they were chosen by.
DARK_PHRASES: Final[dict[str, str]] = {
    "astronomical": "dark hours",
    "nautical": "hours of nautical darkness",
    "none": "hours of the session",
}


def _hhmm(t: datetime) -> str:
    return f"{t:%H:%M} UTC"


def _note(severity: str, text: str) -> schemas.OutlookNoteOut:
    return schemas.OutlookNoteOut(severity=severity, text=text)


# --------------------------------------------------------------------------
# the arithmetic -- pure functions of arrays, so tests can craft a night
# --------------------------------------------------------------------------


def sky_category(mean_cloud: float) -> tuple[str, str]:
    """NWS sky-cover term and its label for a mean cover fraction."""
    for limit, key, label in CATEGORIES:
        if mean_cloud < limit:
            return key, label
    return "overcast", "Overcast"


def verdict_for(clear_slots: int, dark_slots: int, slot_hours: float) -> str:
    clear_h = clear_slots * slot_hours
    if clear_h >= GO_HOURS - 1e-9 or (
        dark_slots > 0 and clear_slots >= GO_FRACTION * dark_slots - 1e-9
    ):
        return "go"
    if clear_h >= MARGINAL_HOURS - 1e-9:
        return "marginal"
    return "poor"


def dark_mask(sun_alt_deg: NDArray[np.float64]) -> tuple[NDArray[np.bool_], str]:
    """Slots that count as night, and which definition of night was used."""
    for limit, name in DARK_LIMITS:
        m = sun_alt_deg < limit
        if bool(m.any()):
            return m, name
    return np.ones(sun_alt_deg.shape, dtype=bool), "none"


def _runs(mask: NDArray[np.bool_]) -> list[tuple[int, int]]:
    """Maximal [a, b) runs of True."""
    out: list[tuple[int, int]] = []
    a: int | None = None
    for i, v in enumerate(mask.tolist()):
        if v and a is None:
            a = i
        elif not v and a is not None:
            out.append((a, i))
            a = None
    if a is not None:
        out.append((a, len(mask)))
    return out


@dataclass(frozen=True, slots=True)
class Window:
    start: int
    """First slot, inclusive."""
    end: int
    """Last slot, exclusive."""
    mean_cloud: float


def best_window(
    cloud: NDArray[np.float64], dark: NDArray[np.bool_], slot_minutes: int
) -> Window | None:
    """The longest clear stretch inside the dark slots (see the module docs)."""
    clear = (cloud < CLEAR_BELOW) & dark
    bridge = max(1, BRIDGE_MINUTES // slot_minutes)
    merged: list[tuple[int, int]] = []
    for a, b in _runs(clear):
        if merged:
            pa, pb = merged[-1]
            gap = a - pb
            if gap <= bridge and gap <= min(pb - pa, b - a) and bool(dark[pb:a].all()):
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))
    if not merged:
        return None

    def score(r: tuple[int, int]) -> tuple[int, float, int]:
        a, b = r
        return (b - a, -float(np.mean(cloud[a:b])), -a)

    a, b = max(merged, key=score)
    if (b - a) * slot_minutes < MIN_WINDOW_MINUTES:
        return None
    return Window(a, b, float(np.mean(cloud[a:b])))


@dataclass(frozen=True, slots=True)
class Trend:
    builds: bool
    first_third: float
    last_third: float
    change_slot: int


def trend(cloud: NDArray[np.float64], dark: NDArray[np.bool_]) -> Trend | None:
    """First third vs last third of the dark slots, and when it turned.

    The turn is the split of the dark slots that maximises the weighted
    difference between the mean after and the mean before -- a one-change-point
    fit, which a brief early blip cannot fool the way a first crossing can.
    """
    idx = np.flatnonzero(dark)
    n = len(idx)
    if n < 3:
        return None
    third = n // 3
    seq = cloud[idx]
    first = float(np.mean(seq[:third]))
    last = float(np.mean(seq[-third:]))
    delta = last - first
    if abs(delta) < TREND_DELTA:
        return None
    csum = np.concatenate([[0.0], np.cumsum(seq)])
    k = np.arange(1, n)
    before = csum[k] / k
    after = (csum[n] - csum[k]) / (n - k)
    stat = np.sign(delta) * (after - before) * np.sqrt(k * (n - k) / n)
    split = int(k[int(np.argmax(stat))])
    return Trend(builds=delta > 0, first_third=first, last_third=last, change_slot=int(idx[split]))


def _masked(arr: NDArray[np.float64] | None, mask: NDArray[np.bool_]) -> NDArray[np.float64] | None:
    if arr is None:
        return None
    vals = arr[mask]
    vals = vals[np.isfinite(vals)]
    return vals if vals.size else None


def _pct(f: float) -> str:
    return f"{round(f * 100)}%"


def _kmh(ms: float) -> str:
    return f"{ms * 3.6:.0f} km/h"


def build_outlook(
    *,
    as_of: datetime,
    grid: TimeGrid,
    cloud: NDArray[np.float64],
    sun_alt_deg: NDArray[np.float64],
    has_data: bool,
    humidity: NDArray[np.float64] | None = None,
    dew_spread_c: NDArray[np.float64] | None = None,
    wind_ms: NDArray[np.float64] | None = None,
    wind_gust_ms: NDArray[np.float64] | None = None,
    basis: str = "",
    caveats: Sequence[schemas.OutlookNoteOut] = (),
    covered: NDArray[np.bool_] | None = None,
    from_slot: int = 0,
) -> schemas.WeatherOutlookOut:
    """The outlook for one night's arrays. ``caveats`` are data notes the
    caller knows about (synthetic, unavailable, seeing); the weather notes are
    derived here.

    ``from_slot`` is the first slot the plan can still change -- its decision
    point's first free slot -- and only dark slots from there on are
    summarised. ``covered`` marks the slots a forecast reaches (all of them
    when omitted); the rest hold the clear-sky assumption and are reported as
    not forecast.
    """
    slot_h = grid.slot_minutes / 60.0
    n = int(cloud.shape[0])
    dark_all, darkness = dark_mask(sun_alt_deg)
    ahead = np.arange(n) >= from_slot
    dark = dark_all & ahead
    # The darkness definition is the night's, not the remainder's: a plan
    # made late in an astronomical night does not switch to nautical wording.
    passed = bool((dark_all & ~ahead).any())
    base_phrase = DARK_PHRASES[darkness]
    phrase = f"remaining {base_phrase}" if passed else base_phrase
    n_dark = int(dark.sum())
    dark_h = n_dark * slot_h

    notes: list[schemas.OutlookNoteOut] = list(caveats)
    if passed and n_dark == 0:
        return _dark_over(as_of, has_data, basis, base_phrase, notes)

    if darkness == "nautical":
        notes.append(
            _note(
                "info",
                "The Sun never gets 18° below the horizon tonight, so this summary covers "
                "nautical darkness (Sun below -12°): favour bright targets and short subs.",
            )
        )
    elif darkness == "none":
        notes.append(
            _note(
                "info",
                "The sky never gets properly dark in this session, so this summary covers "
                "all of it: favour the Moon, planets and bright clusters.",
            )
        )

    if not has_data:
        return _assumed(as_of, basis, "No forecast: the plan assumes a clear sky", dark_h, notes)

    cov = np.ones(n, dtype=bool) if covered is None else np.asarray(covered, dtype=bool)
    notes.extend(_reach_notes(grid, cov, dark, phrase))
    use = dark & cov
    n_use = int(use.sum())
    if n_use == 0:
        return _assumed(
            as_of,
            basis,
            f"No forecast reaches the {phrase}: the plan assumes a clear sky",
            dark_h,
            notes,
        )

    c_use = cloud[use]
    mean_cloud = float(np.mean(c_use))
    n_clear = int(np.count_nonzero(c_use < CLEAR_BELOW))
    n_unc = n_dark - n_use
    clear_h, use_h, unc_h = n_clear * slot_h, n_use * slot_h, n_unc * slot_h
    category, label = sky_category(mean_cloud)
    # Uncovered hours count as not forecast clear. If counting them clear
    # would change the call, the forecast does not decide it.
    low = verdict_for(n_clear, n_dark, slot_h)
    high = verdict_for(n_clear + n_unc, n_dark, slot_h)
    verdict = low if low == high else "unknown"
    win = best_window(cloud, use, grid.slot_minutes)

    partial = n_unc > 0
    over = (
        f"the {use_h:.1f} of {dark_h:.1f} {phrase} the forecast reaches"
        if partial
        else f"the {dark_h:.1f} {phrase}"
    )
    headline = f"{label}: {_pct(mean_cloud)} average cloud over {over}"
    share = f"{clear_h:.1f} of {dark_h:.1f} {phrase} are forecast under 30% cloud"
    gap = f"; the forecast does not reach the other {unc_h:.1f}" if partial else ""
    if verdict == "unknown":
        verdict_text = (
            f"Too soon to call: the forecast reaches only {use_h:.1f} of the {dark_h:.1f} "
            f"{phrase}, and {clear_h:.1f} of those are under 30% cloud. Create the session "
            "again later, when the forecast reaches further."
        )
    elif verdict == "go":
        lead = "Good for the rest of the night" if passed else "Good night to image"
        verdict_text = f"{lead}: {share}{gap}."
    elif verdict == "marginal":
        where = (
            f" The best stretch is {grid.slot_start(win.start):%H:%M} to "
            f"{_hhmm(grid.slot_start(win.end))}: put your priority targets there."
            if win is not None
            else " Keep targets short and flexible."
        )
        verdict_text = f"Marginal: only {share}{gap}.{where}"
    else:
        if n_clear == 0:
            share = (
                f"none of the {use_h:.1f} {phrase} the forecast reaches is under 30% cloud"
                f", and it does not reach the other {unc_h:.1f}"
                if partial
                else f"none of the {dark_h:.1f} {phrase} is forecast under 30% cloud"
            )
        else:
            share = f"only {share}{gap}"
        if passed:
            verdict_text = (
                f"Poor for the rest of the night: {share}. Try bright targets through any "
                "gaps, or finish early."
            )
        else:
            verdict_text = (
                f"Poor night: {share}. Consider another night, or bright targets through any gaps."
            )

    tr = trend(cloud, use)
    if tr is not None:
        at = _hhmm(grid.slot_start(tr.change_slot))
        change = f"{_pct(tr.first_third)} → {_pct(tr.last_third)}"
        if tr.builds:
            notes.append(
                _note(
                    "warn",
                    f"Cloud builds after ~{at} ({change}): image your priority targets first.",
                )
            )
        else:
            notes.append(
                _note(
                    "info",
                    f"Clearing after ~{at} ({change}): save your priority targets for "
                    "later in the night.",
                )
            )

    hum = _masked(humidity, use)
    dew = _masked(dew_spread_c, use)
    wind = _masked(wind_ms, use)
    gust = _masked(wind_gust_ms, use)
    hum_max = float(np.max(hum)) if hum is not None else None
    dew_min = float(np.min(dew)) if dew is not None else None
    wind_max = float(np.max(wind)) if wind is not None else None
    gust_max = float(np.max(gust)) if gust is not None else None

    if (dew_min is not None and dew_min < DEW_SPREAD_C) or (
        hum_max is not None and hum_max >= DEW_HUMIDITY
    ):
        bits = []
        if dew_min is not None:
            bits.append(f"dew-point spread down to {dew_min:.1f} °C")
        if hum_max is not None:
            bits.append(f"humidity up to {_pct(hum_max)}")
        start = "from the start" if not passed else "now"
        notes.append(_note("warn", f"Dew likely ({', '.join(bits)}): run dew heaters {start}."))

    wind_note = _wind_note(wind_max, gust_max)
    if wind_note is not None:
        notes.append(wind_note)

    return schemas.WeatherOutlookOut(
        as_of=as_of,
        has_data=True,
        basis=basis,
        category=category,
        label=label,
        headline=headline,
        verdict=verdict,
        verdict_text=verdict_text,
        dark_hours=round(dark_h, 2),
        clear_hours=round(clear_h, 2),
        mean_cloud_fraction=round(mean_cloud, 4),
        best_window=schemas.OutlookWindowOut(
            starts_at=grid.slot_start(win.start),
            ends_at=grid.slot_start(win.end),
            hours=round((win.end - win.start) * slot_h, 2),
            mean_cloud_fraction=round(win.mean_cloud, 4),
        )
        if win is not None
        else None,
        humidity_max=round(hum_max, 4) if hum_max is not None else None,
        dew_spread_min_c=round(dew_min, 2) if dew_min is not None else None,
        wind_max_ms=round(wind_max, 2) if wind_max is not None else None,
        notes=_sorted(notes),
    )


def _reach_notes(
    grid: TimeGrid, cov: NDArray[np.bool_], dark: NDArray[np.bool_], phrase: str
) -> list[schemas.OutlookNoteOut]:
    """Critical notes for the summarised hours no forecast reaches."""
    unc = dark & ~cov
    if not bool(unc.any()):
        return []
    idx = np.flatnonzero(cov)
    if idx.size == 0:
        return [
            _note(
                "critical",
                f"The forecast does not reach the {phrase}: the plan assumes a clear sky "
                "for all of them.",
            )
        ]
    first, last = int(idx[0]), int(idx[-1])
    out: list[schemas.OutlookNoteOut] = []
    if bool(unc[:first].any()):
        out.append(
            _note(
                "critical",
                f"The forecast starts only at {_hhmm(grid.slot_start(first))}; before that "
                "the plan assumes a clear sky.",
            )
        )
    if bool(unc[last + 1 :].any()):
        out.append(
            _note(
                "critical",
                f"The forecast reaches only {_hhmm(grid.slot_start(last + 1))}; after that "
                "the plan assumes a clear sky.",
            )
        )
    return out


def _assumed(
    as_of: datetime,
    basis: str,
    headline: str,
    dark_h: float,
    notes: list[schemas.OutlookNoteOut],
) -> schemas.WeatherOutlookOut:
    """No forecast for the summarised hours: every number is the assumption."""
    return schemas.WeatherOutlookOut(
        as_of=as_of,
        has_data=False,
        basis=basis,
        category="unknown",
        label="No forecast",
        headline=headline,
        verdict="unknown",
        verdict_text=(
            "Nothing here says whether it will be clear."
        ),
        dark_hours=round(dark_h, 2),
        # What the plan ASSUMED, per the contract: every dark hour clear.
        clear_hours=round(dark_h, 2),
        mean_cloud_fraction=0.0,
        best_window=None,
        humidity_max=None,
        dew_spread_min_c=None,
        wind_max_ms=None,
        notes=_sorted(notes),
    )


def _dark_over(
    as_of: datetime,
    has_data: bool,
    basis: str,
    base_phrase: str,
    notes: list[schemas.OutlookNoteOut],
) -> schemas.WeatherOutlookOut:
    """A plan made after the last dark slot: nothing left to summarise."""
    return schemas.WeatherOutlookOut(
        as_of=as_of,
        has_data=has_data,
        basis=basis,
        category="unknown",
        label="Night over",
        headline=f"The {base_phrase} are over",
        verdict="unknown",
        verdict_text=(
            "This plan starts after the last dark slot: the rest of the session is "
            "twilight, too bright for deep-sky imaging."
        ),
        dark_hours=0.0,
        clear_hours=0.0,
        mean_cloud_fraction=0.0,
        best_window=None,
        humidity_max=None,
        dew_spread_min_c=None,
        wind_max_ms=None,
        notes=_sorted(notes),
    )


def _wind_note(wind_max: float | None, gust_max: float | None) -> schemas.OutlookNoteOut | None:
    w = wind_max if wind_max is not None else 0.0
    g = gust_max if gust_max is not None else 0.0
    if w >= WIND_CRIT_MS or g >= GUST_CRIT_MS:
        severity = "critical"
    elif w >= WIND_WARN_MS or g >= GUST_WARN_MS:
        severity = "warn"
    else:
        return None
    parts = []
    if wind_max is not None:
        parts.append(f"wind up to {wind_max:.0f} m/s ({_kmh(wind_max)})")
    if gust_max is not None:
        parts.append(f"gusts to {gust_max:.0f} m/s ({_kmh(gust_max)})")
    what = ", ".join(parts)
    if severity == "critical":
        return _note(
            "critical",
            f"Strong wind ({what}): a long focal length will shake. Image only with a "
            "short, wide-field rig in shelter, or keep the telescope covered.",
        )
    return _note(
        "warn",
        f"Breezy ({what}): shield the telescope from the wind, keep subs short, and "
        "check frames for trailed stars.",
    )


def _sorted(notes: Sequence[schemas.OutlookNoteOut]) -> list[schemas.OutlookNoteOut]:
    return sorted(notes, key=lambda n: _SEVERITY_ORDER.get(n.severity, 3))


# --------------------------------------------------------------------------
# session glue
# --------------------------------------------------------------------------


def _model(sess: NightSession) -> str:
    rep = sess.weather_report
    return model_name(rep.model if rep is not None else (sess.weather.model or "ecmwf_ifs025"))


def _when(t: datetime, ref: datetime) -> str:
    """HH:MM UTC, with the day when it is not ``ref``'s day."""
    return _hhmm(t) if t.date() == ref.date() else fmt_utc(t)


def _basis(sess: NightSession, dp: DecisionPoint) -> str:
    """The newest run this plan used, and when it was published."""
    refs = dp.conditions.ledger.refs
    if not refs:
        return ""
    newest = max(refs, key=lambda r: r.published_at)
    pub = newest.published_at
    name = _model(sess)
    if newest.provider == LIVE_SOURCE_ID:
        rep = dp.report or sess.weather_report
        run = (
            f" (newest model run {fmt_utc(rep.live_run)})"
            if rep is not None and rep.live_run is not None
            else ""
        )
        return f"Live {name} forecast via Open-Meteo{run}, fetched {_when(pub, dp.at)}"
    if newest.provider == RUNS_SOURCE_ID:
        init = run_of(newest.record_id)
        of = f" of {fmt_utc(init)}" if init is not None else ""
        return (
            f"{name} run{of}, published ~{_when(pub, dp.at)} "
            "(estimated conservatively from Open-Meteo's delivery delay)"
        )
    if newest.provider == FixtureForecast.source_id:
        return f"Synthetic demo run issued {_when(pub, dp.at)}"
    return f"{newest.provider}, published {_when(pub, dp.at)}"


def _live_known(sess: NightSession, dp: DecisionPoint, rep: FetchReport) -> bool:
    """Whether the live fetch behind ``rep`` concerns the plan at ``dp``.

    It does if the plan was made at or after the fetch, or if it is the plan
    that fetch would have replaced -- the one in force when it was made, which
    is the one the observer is holding. An earlier plan was superseded by a
    later one before the fetch was ever attempted, so its failure is not news
    to it.
    """
    t = rep.now
    if t is None or dp.at >= t:
        return True
    return not any(dp.at < d.at <= t for d in sess.decision_points)


def _caveats(sess: NightSession, dp: DecisionPoint, has_data: bool) -> list[schemas.OutlookNoteOut]:
    mode = sess.weather_mode
    rep = dp.report or sess.weather_report
    out: list[schemas.OutlookNoteOut] = []
    # Only what a plan made at dp.at could have known: a run that failed, or
    # predates the archive, only if it would have been published by then.
    live = rep is not None and _live_known(sess, dp, rep)
    deg = rep.degradation(dp.at, live=live) if rep is not None else None

    if mode is WeatherMode.SYNTHETIC:
        out.append(
            _note(
                "warn",
                "Synthetic demo weather, not a forecast.",
            )
        )
        return out

    if not has_data:
        if mode is WeatherMode.UNAVAILABLE:
            why = sess.weather_unavailable_reason or "no forecast record reached any plan"
            out.append(
                _note(
                    "critical",
                    f"No forecast: {why}. The plan assumes a clear sky.",
                )
            )
        else:
            why = f" ({deg})" if deg else ""
            out.append(
                _note(
                    "critical",
                    f"No forecast had reached this plan by {_hhmm(dp.at)}{why}, so it assumes "
                    "a clear sky; plans later in the night use real forecast data.",
                )
            )
        return out

    if rep is not None:
        if deg is not None and live and rep.live_failed:
            out.append(
                _note(
                    "warn",
                    f"Not the live forecast: {deg}. This plan uses the newest archived "
                    "run instead; create the session again later for the latest forecast.",
                )
            )
        elif deg is not None:
            out.append(
                _note("info", f"Some data is missing: {deg}. Plans use the newest run that was.")
            )
    return out


def outlook_out(sess: NightSession, dp: DecisionPoint) -> schemas.WeatherOutlookOut:
    grid = sess.spec.grid
    cond = dp.conditions
    geo = sess.geometry
    sun = geo.sun_altitude_deg if geo is not None else np.full(grid.n_slots, -90.0)
    has_data = bool(cond.ledger.refs)
    return build_outlook(
        as_of=dp.at,
        grid=grid,
        cloud=cond.cloud_fraction,
        sun_alt_deg=sun,
        has_data=has_data,
        humidity=cond.humidity,
        dew_spread_c=cond.dew_spread_c,
        wind_ms=cond.wind_ms,
        wind_gust_ms=cond.wind_gust_ms,
        basis=_basis(sess, dp),
        caveats=_caveats(sess, dp, has_data),
        covered=_coverage(cond),
        # The first slot this plan can still change: the slot containing its
        # decision point, as the scheduler locks it. Advice about earlier
        # slots would be advice about the past.
        from_slot=dp.locked_through_slot,
    )


def _coverage(cond: ConditionsLayer) -> NDArray[np.bool_]:
    """Which slots a forecast reaches; a layer without the mask is covered
    wherever it has records at all."""
    if cond.covered is not None:
        return cond.covered
    return np.full(cond.n_slots, bool(cond.ledger.refs))


def _per_slot(
    values: NDArray[np.float64] | None, slots: Sequence[int], ndigits: int
) -> list[float | None]:
    """One value per plan slot; ``None`` where the forecast said nothing."""
    if values is None:
        return [None] * len(slots)
    out: list[float | None] = []
    for s in slots:
        v = float(values[s]) if 0 <= s < values.shape[0] else float("nan")
        out.append(round(v, ndigits) if np.isfinite(v) else None)
    return out


def slot_weather_out(sess: NightSession, dp: DecisionPoint) -> schemas.SlotWeatherOut:
    """The forecast in force at ``dp``, one value per slot of its plan.

    Read from the same conditions layer as ``outlook_out`` -- which the
    publication gate bounded by ``dp``'s as_of -- so an early plan shows the
    run published by then, never a later one, and ``basis`` is the outlook's
    wording for that run. A field the source does not give (all of them, for
    synthetic weather or no forecast) is ``None`` in every slot. Seeing is
    never forecast: ``seeing_forecast`` stays False.

    A slot no forecast reaches (``covered`` False: past the forecast's last
    hour, or no forecast at all) is ``None`` in every array, whatever the
    arrays hold there.
    """
    c = dp.conditions
    slots = [a.slot for a in dp.plan.assignments]
    cov = _coverage(c)
    covered = [bool(cov[s]) if 0 <= s < cov.shape[0] else False for s in slots]

    def per(values: NDArray[np.float64] | None, ndigits: int) -> list[float | None]:
        vals = _per_slot(values, slots, ndigits)
        return [v if ok else None for v, ok in zip(vals, covered, strict=True)]

    return schemas.SlotWeatherOut(
        temperature_c=per(c.temperature_c, 2),
        dew_point_c=per(c.dew_point_c, 2),
        humidity=per(c.humidity, 4),
        wind_ms=per(c.wind_ms, 2),
        wind_gust_ms=per(c.wind_gust_ms, 2),
        precipitation_mm=per(c.precipitation_mm, 3),
        cloud_low=per(c.cloud_low, 4),
        cloud_mid=per(c.cloud_mid, 4),
        cloud_high=per(c.cloud_high, 4),
        covered=covered,
        seeing_forecast=False,
        basis=_basis(sess, dp) or None,
    )


# --------------------------------------------------------------------------
# the source
# --------------------------------------------------------------------------


def _evidence_counts(sess: NightSession) -> tuple[int, int]:
    """Distinct runs and records that reached at least one plan."""
    runs: set[tuple[str, str]] = set()
    records: set[tuple[str, str]] = set()
    for dp in sess.decision_points:
        for r in dp.conditions.ledger.refs:
            records.add((r.provider, r.record_id))
            runs.add((r.provider, r.record_id.rsplit(":", 1)[0]))
    return len(runs), len(records)


def _description(sess: NightSession, mode: WeatherMode, rep: FetchReport | None) -> str:
    name = _model(sess)
    res = sess.weather
    if mode is WeatherMode.SYNTHETIC:
        return (
            "Offline demo weather: scripted model runs in which a front arrives late in "
            "the night. Not a forecast."
        )
    if mode is WeatherMode.UNAVAILABLE:
        why = sess.weather_unavailable_reason or "no forecast record reached any plan"
        return f"No forecast covers this night: {why}. The plan assumes a clear sky."
    if mode is WeatherMode.ARCHIVE:
        if res.timing is NightTiming.PAST or res.timing is None:
            text = (
                f"This night is over: the {name} model runs via Open-Meteo that had been "
                "published by each moment of it, replayed in order, so each plan sees "
                "only what was known then."
            )
        else:
            text = (
                f"Archived {name} model runs via Open-Meteo, only those published by "
                f"{fmt_utc(res.now) if res.now else 'the session start'}, replayed in order."
            )
        deg = rep.degradation() if rep is not None else None
        return f"{text} Note: {deg}." if deg else text

    # forecast
    if rep is None:
        return f"The live {name} forecast via Open-Meteo, fetched as the session is built."
    if rep.live_fetched_at is None:
        why = rep.live_error or rep.halt_detail or "no data"
        when = fmt_utc(res.now) if res.now is not None else "the session start"
        return (
            f"The live {name} forecast could not be fetched ({why}), so plans use the "
            f"newest {name} run via Open-Meteo published by {when}."
        )
    fetched = fmt_utc(rep.live_fetched_at)
    if res.timing is NightTiming.IN_PROGRESS:
        return (
            f"The live {name} forecast via Open-Meteo, fetched {fetched}; plans earlier "
            "in the night use the model runs that had been published by then."
        )
    return f"The live {name} forecast via Open-Meteo, fetched {fetched}."


def weather_source_out(sess: NightSession) -> schemas.WeatherSourceOut:
    mode = sess.weather_mode
    rep = sess.weather_report
    runs, records = _evidence_counts(sess)
    return schemas.WeatherSourceOut(
        requested=sess.weather.requested,
        mode=mode.value,
        label=SOURCE_LABELS[mode],
        description=_description(sess, mode, rep),
        model=sess.weather.model if mode is not WeatherMode.SYNTHETIC else None,
        fetched_at=rep.live_fetched_at
        if mode is WeatherMode.FORECAST and rep is not None
        else None,
        runs=runs,
        records=records,
    )
