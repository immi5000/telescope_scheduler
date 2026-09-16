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
    valid_time: datetime
    cloud_cover: float
    """Total, 0-1. A COVERAGE fraction, not an opacity -- see physics/cloud.py."""
    cloud_low: float = 0.0
    cloud_mid: float = 0.0
    cloud_high: float = 0.0
    seeing_fwhm_arcsec: float = 2.5
    humidity: float = 0.5
    dewpoint_spread_c: float = 5.0
    wind_speed_ms: float = 2.0
