"""The weather outlook, on crafted nights.

Every number and sentence the Outlook panel shows comes from
``build_outlook``, which is a pure function of the slot arrays. So each rule --
sky-cover bands, verdict thresholds, the best window, trend, dew, wind, and the
no-data wording -- is pinned here on a night built to sit on its edge.

The night: 10 hours from 00:00 UTC on 5-minute slots, with one hour of
twilight at each end and eight dark hours (slots 12-107) between.

Two inputs narrow what is summarised: ``from_slot``, the first slot a plan
can still change (so a plan made at 06:00 is not advised about 01:00), and
``covered``, the slots a forecast reaches (past it the plan assumes a clear
sky, and the outlook must not call that a forecast).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from tscheduler.api import schemas
from tscheduler.api.outlook import best_window, build_outlook, sky_category, trend
from tscheduler.core.timegrid import TimeGrid

START = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)
GRID = TimeGrid(START, 120, 5)
DARK_FROM, DARK_TO = 12, 108  # 8 dark hours
SUN = np.full(120, -10.0)
SUN[DARK_FROM:DARK_TO] = -30.0
DARK = SUN < -18.0


def night(value: float, **spans: float) -> np.ndarray:
    """A constant-cloud night, with ``s{a}_{b}=v`` overriding slots [a, b)."""
    c = np.full(120, value)
    for key, v in spans.items():
        a, b = (int(x) for x in key[1:].split("_"))
        c[a:b] = v
    return c


def outlook(cloud: np.ndarray, **kw: Any) -> schemas.WeatherOutlookOut:
    args: dict[str, Any] = {
        "as_of": START,
        "grid": GRID,
        "cloud": cloud,
        "sun_alt_deg": SUN,
        "has_data": True,
    }
    args.update(kw)
    return build_outlook(**args)


def texts(o: schemas.WeatherOutlookOut, severity: str | None = None) -> list[str]:
    return [n.text for n in o.notes if severity is None or n.severity == severity]


# --------------------------------------------------------------------------
# sky cover
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cover", "category", "label"),
    [
        (0.0, "clear", "Clear"),
        (0.124, "clear", "Clear"),
        (0.126, "mostly-clear", "Mostly clear"),
        (0.374, "mostly-clear", "Mostly clear"),
        (0.376, "partly-cloudy", "Partly cloudy"),
        (0.624, "partly-cloudy", "Partly cloudy"),
        (0.626, "mostly-cloudy", "Mostly cloudy"),
        (0.874, "mostly-cloudy", "Mostly cloudy"),
        (0.876, "overcast", "Overcast"),
        (1.0, "overcast", "Overcast"),
    ],
)
def test_nws_sky_cover_bands_in_eighths(cover: float, category: str, label: str) -> None:
    assert sky_category(cover) == (category, label)
    o = outlook(night(cover))
    assert (o.category, o.label) == (category, label)


def test_headline_names_the_cover_and_the_dark_hours() -> None:
    o = outlook(night(0.18))
    assert o.headline == "Mostly clear: 18% average cloud over the 8.0 dark hours"
    assert o.dark_hours == 8.0
    assert o.mean_cloud_fraction == pytest.approx(0.18)


def test_only_the_dark_slots_are_summarised() -> None:
    """Cloud in civil twilight is not what anyone setting up is asking about."""
    o = outlook(night(0.05, s0_12=1.0, s108_120=1.0))
    assert o.category == "clear"
    assert o.mean_cloud_fraction == pytest.approx(0.05)


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------


def test_all_clear_is_go_with_a_plain_sentence() -> None:
    o = outlook(night(0.1))
    assert o.verdict == "go"
    assert o.clear_hours == 8.0
    assert o.verdict_text == (
        "Good night to image: 8.0 of 8.0 dark hours are forecast under 30% cloud."
    )


def test_four_clear_hours_is_go_even_under_sixty_percent() -> None:
    o = outlook(night(0.8, s12_60=0.1))  # 48 slots = 4.0 h of 8
    assert o.clear_hours == 4.0
    assert o.verdict == "go"


def test_under_four_hours_and_sixty_percent_is_marginal_and_says_where() -> None:
    o = outlook(night(0.8, s12_54=0.1))  # 3.5 h
    assert o.verdict == "marginal"
    assert o.verdict_text.startswith("Marginal: only 3.5 of 8.0 dark hours")
    assert "01:00 to 04:30 UTC" in o.verdict_text


def test_one_clear_hour_is_marginal_and_less_is_poor() -> None:
    assert outlook(night(0.8, s40_52=0.1)).verdict == "marginal"  # exactly 1 h
    poor = outlook(night(0.8, s40_51=0.1))  # 55 minutes
    assert poor.verdict == "poor"
    assert poor.verdict_text.startswith("Poor night: only 0.9 of 8.0 dark hours")


def test_a_night_with_no_clear_hour_says_none() -> None:
    o = outlook(night(0.95))
    assert o.verdict == "poor"
    assert "none of the 8.0 dark hours" in o.verdict_text
    assert o.best_window is None


def test_sixty_percent_of_a_short_night_is_go() -> None:
    sun = np.full(120, -10.0)
    sun[12:72] = -30.0  # 5 dark hours
    go = outlook(night(0.8, s12_48=0.1), sun_alt_deg=sun)  # 3.0 h = 60%
    marginal = outlook(night(0.8, s12_47=0.1), sun_alt_deg=sun)  # 2.92 h
    assert (go.dark_hours, go.verdict) == (5.0, "go")
    assert marginal.verdict == "marginal"


# --------------------------------------------------------------------------
# best window
# --------------------------------------------------------------------------


def test_best_window_is_the_longest_clear_dark_stretch() -> None:
    o = outlook(night(0.7, s20_40=0.1, s50_90=0.2))
    w = o.best_window
    assert w is not None
    assert w.starts_at == GRID.slot_start(50)
    assert w.ends_at == GRID.slot_start(90)
    assert w.hours == pytest.approx(40 * 5 / 60, abs=0.01)
    assert w.mean_cloud_fraction == pytest.approx(0.2)


def test_a_single_slot_blip_does_not_split_the_window() -> None:
    cloud = night(0.7, s30_70=0.1)
    cloud[50] = 0.35
    w = best_window(cloud, DARK, 5)
    assert w is not None and (w.start, w.end) == (30, 70)
    assert w.mean_cloud == pytest.approx((39 * 0.1 + 0.35) / 40)


def test_a_fifteen_minute_gap_does_split_it() -> None:
    cloud = night(0.7, s30_70=0.1)
    cloud[45:48] = 0.5
    w = best_window(cloud, DARK, 5)
    assert w is not None and (w.start, w.end) == (48, 70)


def test_no_window_outside_the_dark_or_shorter_than_fifteen_minutes() -> None:
    assert best_window(night(0.9, s0_12=0.0), DARK, 5) is None  # twilight only
    assert best_window(night(0.9, s40_42=0.0), DARK, 5) is None  # 10 minutes
    assert best_window(night(0.9, s40_43=0.0), DARK, 5) is not None  # 15 minutes


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------


def test_cloud_building_says_when_and_what_to_do() -> None:
    o = outlook(night(0.8, s12_60=0.1))  # front at slot 60 = 05:00 UTC
    warn = texts(o, "warn")
    assert any(
        t.startswith("Cloud builds after ~05:00 UTC") and "image your priority targets first" in t
        for t in warn
    ), warn


def test_clearing_says_when_and_what_to_do() -> None:
    o = outlook(night(0.1, s12_60=0.8))
    assert any(
        t.startswith("Clearing after ~05:00 UTC") and "later in the night" in t
        for t in texts(o, "info")
    )


def test_an_early_blip_does_not_move_the_turn() -> None:
    cloud = night(0.9, s12_80=0.1)
    cloud[20] = 0.9
    tr = trend(cloud, DARK)
    assert tr is not None and tr.builds
    assert tr.change_slot == 80


def test_a_small_change_is_not_a_trend() -> None:
    assert trend(night(0.3, s12_60=0.1), DARK) is None
    assert not any("Cloud builds" in t or "Clearing" in t for t in texts(outlook(night(0.2))))


def test_dew_from_a_narrow_spread() -> None:
    o = outlook(night(0.1), dew_spread_c=np.full(120, 1.5), humidity=np.full(120, 0.8))
    warn = texts(o, "warn")
    assert any("Dew likely" in t and "run dew heaters from the start" in t for t in warn)
    assert o.dew_spread_min_c == pytest.approx(1.5)
    assert o.humidity_max == pytest.approx(0.8)


def test_dew_from_high_humidity() -> None:
    o = outlook(night(0.1), dew_spread_c=np.full(120, 4.0), humidity=np.full(120, 0.96))
    assert any("Dew likely" in t and "humidity up to 96%" in t for t in texts(o, "warn"))


def test_no_dew_note_on_a_dry_night_and_twilight_is_ignored() -> None:
    spread = np.full(120, 6.0)
    spread[:12] = 0.5  # dew in twilight, before the dark hours
    o = outlook(night(0.1), dew_spread_c=spread, humidity=np.full(120, 0.6))
    assert not any("Dew" in t for t in texts(o))
    assert o.dew_spread_min_c == pytest.approx(6.0)


def test_wind_warns_at_eight_metres_per_second() -> None:
    o = outlook(night(0.1), wind_ms=np.full(120, 9.0))
    warn = texts(o, "warn")
    assert any("9 m/s (32 km/h)" in t and "shield the telescope" in t for t in warn), warn
    assert o.wind_max_ms == pytest.approx(9.0)


def test_wind_is_critical_at_twelve() -> None:
    o = outlook(night(0.1), wind_ms=np.full(120, 13.0))
    crit = texts(o, "critical")
    assert any("13 m/s (47 km/h)" in t for t in crit), crit


def test_strong_gusts_alone_are_critical() -> None:
    o = outlook(night(0.1), wind_ms=np.full(120, 5.0), wind_gust_ms=np.full(120, 18.0))
    crit = texts(o, "critical")
    assert any("gusts to 18 m/s (65 km/h)" in t for t in crit), crit


def test_calm_night_has_no_wind_note() -> None:
    o = outlook(night(0.1), wind_ms=np.full(120, 5.0), wind_gust_ms=np.full(120, 8.0))
    assert not any("wind" in t.lower() for t in texts(o))


def test_missing_fields_are_none_not_invented() -> None:
    o = outlook(night(0.1))
    assert o.humidity_max is None and o.dew_spread_min_c is None and o.wind_max_ms is None


def test_notes_come_most_severe_first() -> None:
    o = outlook(
        night(0.8, s12_60=0.1),
        wind_ms=np.full(120, 13.0),
        caveats=[schemas.OutlookNoteOut(severity="info", text="caveat")],
    )
    order = [n.severity for n in o.notes]
    assert order == sorted(order, key=["critical", "warn", "info"].index)
    assert order[0] == "critical"


# --------------------------------------------------------------------------
# no data, and nights that never get dark
# --------------------------------------------------------------------------


def test_no_data_is_unknown_and_says_the_sky_is_assumed_clear() -> None:
    o = outlook(np.zeros(120), has_data=False)
    assert o.has_data is False
    assert (o.category, o.label, o.verdict) == ("unknown", "No forecast", "unknown")
    assert o.headline == "No forecast: the plan assumes a clear sky"
    assert o.verdict_text == "Nothing here says whether it will be clear."
    assert o.best_window is None
    assert o.humidity_max is None and o.wind_max_ms is None and o.dew_spread_min_c is None
    # The numbers describe the ASSUMED clear sky, as the contract says.
    assert o.clear_hours == o.dark_hours == 8.0


def test_a_night_without_astronomical_darkness_uses_nautical() -> None:
    sun = np.full(120, -8.0)
    sun[30:90] = -15.0
    o = outlook(night(0.1), sun_alt_deg=sun)
    assert o.dark_hours == 5.0
    assert o.headline.endswith("over the 5.0 hours of nautical darkness")
    assert any("nautical darkness" in t for t in texts(o, "info"))


def test_a_night_that_never_gets_dark_covers_the_whole_session() -> None:
    o = outlook(night(0.1), sun_alt_deg=np.full(120, -5.0))
    assert o.dark_hours == 10.0
    assert any("never gets properly dark" in t for t in texts(o, "info"))


def test_a_nautical_night_says_nautical_in_the_verdict_too() -> None:
    sun = np.full(120, -8.0)
    sun[30:90] = -15.0
    go = outlook(night(0.1), sun_alt_deg=sun)
    assert "5.0 of 5.0 hours of nautical darkness are forecast" in go.verdict_text
    poor = outlook(night(0.95), sun_alt_deg=sun)
    assert "none of the 5.0 hours of nautical darkness" in poor.verdict_text
    assert "dark hours" not in go.verdict_text + poor.verdict_text


# --------------------------------------------------------------------------
# the rest of the night: a plan is advised only about what it can still change
# --------------------------------------------------------------------------


def test_a_plan_made_mid_night_is_not_advised_about_the_past() -> None:
    """Clear 01:00-04:00, cloudy after; the plan is made at 06:00. The clear
    stretch is history: no best window there, no "cloud builds" advice about
    it, and the verdict is about 06:00 onwards."""
    cloud = night(0.8, s12_48=0.1)
    o = outlook(cloud, as_of=GRID.slot_start(72), from_slot=72)
    assert o.dark_hours == 3.0  # slots 72-107
    assert o.clear_hours == 0.0
    assert o.best_window is None
    assert o.verdict == "poor"
    assert o.verdict_text.startswith(
        "Poor for the rest of the night: none of the 3.0 remaining dark hours"
    ), o.verdict_text
    assert o.headline.endswith("over the 3.0 remaining dark hours")
    assert not any("Cloud builds" in t or "Clearing" in t for t in texts(o))
    # The same night from dusk still says what it did.
    dusk = outlook(cloud)
    assert dusk.best_window is not None and dusk.verdict == "marginal"
    assert any(t.startswith("Cloud builds after ~04:00 UTC") for t in texts(dusk, "warn"))


def test_the_best_window_and_trend_are_found_in_what_remains() -> None:
    cloud = night(0.8, s12_30=0.1, s80_100=0.1)
    o = outlook(cloud, from_slot=60)
    assert o.best_window is not None
    assert (o.best_window.starts_at, o.best_window.ends_at) == (
        GRID.slot_start(80),
        GRID.slot_start(100),
    )


def test_dew_and_wind_that_are_over_are_not_warned_about() -> None:
    wind = np.full(120, 3.0)
    wind[:60] = 14.0
    spread = np.full(120, 6.0)
    spread[:60] = 0.5
    o = outlook(night(0.1), wind_ms=wind, dew_spread_c=spread, from_slot=60)
    assert o.wind_max_ms == pytest.approx(3.0)
    assert o.dew_spread_min_c == pytest.approx(6.0)
    assert not any("wind" in t.lower() or "Dew" in t for t in texts(o))
    # And one still to come is.
    later = outlook(night(0.1), wind_ms=np.full(120, 14.0), from_slot=60)
    assert any("Strong wind" in t for t in texts(later, "critical"))


def test_remaining_only_once_dark_hours_are_behind_the_plan() -> None:
    """A plan made in evening twilight still has every dark hour ahead of it."""
    o = outlook(night(0.1), from_slot=6)
    assert o.dark_hours == 8.0
    assert "remaining" not in o.headline + o.verdict_text
    assert o.verdict_text.startswith("Good night to image")
    mid = outlook(night(0.1), from_slot=60)
    assert mid.verdict_text == (
        "Good for the rest of the night: 4.0 of 4.0 remaining dark hours are forecast "
        "under 30% cloud."
    )


def test_a_plan_made_after_the_last_dark_slot_says_the_dark_hours_are_over() -> None:
    caveat = schemas.OutlookNoteOut(severity="info", text="caveat")
    o = outlook(night(0.9), wind_ms=np.full(120, 14.0), from_slot=110, caveats=[caveat])
    assert o.headline == "The dark hours are over"
    assert (o.verdict, o.category) == ("unknown", "unknown")
    assert o.dark_hours == 0.0 and o.clear_hours == 0.0 and o.best_window is None
    assert o.wind_max_ms is None
    assert texts(o) == ["caveat"], "no weather advice about a night that is over"


def test_no_forecast_mid_night_counts_the_remaining_hours() -> None:
    o = outlook(np.zeros(120), has_data=False, from_slot=60)
    assert o.has_data is False
    assert o.dark_hours == o.clear_hours == 4.0


# --------------------------------------------------------------------------
# coverage: hours no forecast reaches are the assumption, not a forecast
# --------------------------------------------------------------------------


def covered_below(k: int) -> np.ndarray:
    c = np.zeros(120, dtype=bool)
    c[:k] = True
    return c


def test_a_partly_covered_night_is_not_reported_as_fully_forecast() -> None:
    """The forecast stops at 03:20; the plan assumes a clear sky after that.
    That is not "8.0 of 8.0 dark hours forecast clear", and not a go."""
    cloud = night(0.1)
    cloud[40:] = 0.0  # the clear-sky assumption, as weather_series fills it
    o = outlook(cloud, covered=covered_below(40))
    assert o.dark_hours == 8.0
    assert o.clear_hours == pytest.approx(28 * 5 / 60, abs=0.01)  # slots 12-39
    assert "The forecast reaches only 03:20 UTC; after that the plan assumes a clear sky." in (
        texts(o, "critical")
    )
    assert (
        o.headline == "Clear: 10% average cloud over the 2.3 of 8.0 dark hours the forecast reaches"
    )
    assert o.verdict == "unknown"
    assert o.verdict_text.startswith(
        "Too soon to call: the forecast reaches only 2.3 of the 8.0 dark hours"
    ), o.verdict_text
    assert "8.0 of 8.0" not in o.verdict_text
    assert o.best_window is not None and o.best_window.ends_at == GRID.slot_start(40)


def test_a_mostly_covered_night_keeps_its_verdict_and_says_what_is_missing() -> None:
    cloud = night(0.1)
    cloud[100:] = 0.0
    o = outlook(cloud, covered=covered_below(100))
    assert o.verdict == "go"
    assert o.verdict_text == (
        "Good night to image: 7.3 of 8.0 dark hours are forecast under 30% cloud; "
        "the forecast does not reach the other 0.7."
    )


def test_uncovered_hours_do_not_make_a_cloudy_night_look_clearer() -> None:
    cloud = night(0.9)
    cloud[104:] = 0.0
    o = outlook(cloud, covered=covered_below(104))
    assert o.mean_cloud_fraction == pytest.approx(0.9)
    assert o.category == "overcast" and o.verdict == "poor"
    assert not any("Clearing" in t for t in texts(o)), "a fake clearing from the assumption"
    assert o.verdict_text.startswith(
        "Poor night: none of the 7.7 dark hours the forecast reaches is under 30% cloud, "
        "and it does not reach the other 0.3."
    ), o.verdict_text


def test_a_forecast_that_misses_every_dark_hour_is_no_forecast() -> None:
    o = outlook(night(0.5), covered=covered_below(10))
    assert o.has_data is False
    assert (o.verdict, o.category) == ("unknown", "unknown")
    assert o.headline == "No forecast reaches the dark hours: the plan assumes a clear sky"
    assert "The forecast reaches only 00:50 UTC; after that the plan assumes a clear sky." in (
        texts(o, "critical")
    )


def test_a_forecast_that_starts_late_says_so() -> None:
    cloud = night(0.1)
    cloud[:30] = 0.0
    covered = np.zeros(120, dtype=bool)
    covered[30:] = True
    o = outlook(cloud, covered=covered)
    assert (
        "The forecast starts only at 02:30 UTC; before that the plan assumes a clear sky."
        in texts(o, "critical")
    )
