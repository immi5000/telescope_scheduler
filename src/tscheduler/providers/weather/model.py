"""Weather value types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class WeatherQuery:
    lat: float
    lon: float
    valid_from: datetime
    valid_to: datetime
    model: str = "ecmwf_ifs025"


@dataclass(frozen=True, slots=True)
class WeatherSample:
    """One forecast hour.

    Everything but total cloud (and the seeing assumption) is optional, and
    ``None`` means *the source did not say*. It used to default to plausible
    numbers (50% humidity, 2 m/s wind, 0% low cloud), which the outlook and the
    per-slot weather would then have shown as if they were a forecast.
    """

    valid_time: datetime
    cloud_cover: float
    """Total, 0-1. A COVERAGE fraction, not an opacity -- see physics/cloud.py."""
    cloud_low: float | None = None
    """Low cloud (below ~2 km), 0-1."""
    cloud_mid: float | None = None
    """Mid cloud (~2-6 km), 0-1."""
    cloud_high: float | None = None
    """High cloud (above ~6 km), 0-1."""
    seeing_fwhm_arcsec: float = 2.5
    """No source we use forecasts seeing; 2.5 arcsec is the planning assumption."""
    humidity: float | None = None
    """Relative humidity at 2 m, 0-1."""
    dewpoint_spread_c: float | None = None
    """Air temperature minus dew point at 2 m. Near zero means dew on the optics."""
    wind_speed_ms: float | None = None
    """Mean wind at 10 m, m/s."""
    wind_gusts_ms: float | None = None
    """Gusts at 10 m, m/s. What actually shakes a telescope. The maximum over
    the hour ENDING at ``valid_time`` (Open-Meteo's "preceding hour max"), so
    ``weather_series`` places it mid-hour, like precipitation."""
    temperature_c: float | None = None
    """Air temperature at 2 m, degrees C."""
    dew_point_c: float | None = None
    """Dew point at 2 m, degrees C."""
    precipitation_mm: float | None = None
    """Rain, showers and snow (as water) over the hour ENDING at ``valid_time``,
    in mm -- Open-Meteo's convention for ``precipitation``. Not a rate at
    ``valid_time``; ``weather_series`` places it mid-hour."""
