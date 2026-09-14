"""Payload builders shared by the test modules.

Measurements are built relative to ``dt_util.utcnow()`` so they are fresh under
real and under frozen time. Hard-coded timestamps would silently expire: a
patient's entities now go unavailable once their own reading is older than
``MAX_MEASUREMENT_AGE``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

MG_DL_PER_MMOL_L = 18.0182


def libre_timestamp(moment: datetime) -> str:
    """Format a moment the way LibreLinkUp does: M/D/YYYY h:mm:ss AM/PM in UTC."""
    hour = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"

    return (
        f"{moment.month}/{moment.day}/{moment.year} "
        f"{hour}:{moment.minute:02d}:{moment.second:02d} {meridiem}"
    )


def measurement(
    *,
    minutes_ago: float = 0.0,
    value: int = 115,
    trend: object = 3,
    **overrides: object,
) -> dict:
    """A glucoseMeasurement as LibreLinkUp returns it."""
    measured_at = dt_util.utcnow() - timedelta(minutes=minutes_ago)

    data: dict = {
        # Follows the display unit of the LibreLinkUp account and is therefore
        # never the source of the Home Assistant state.
        "Value": round(value / MG_DL_PER_MMOL_L, 1),
        "ValueInMgPerDl": value,
        "TrendArrow": trend,
        "Timestamp": libre_timestamp(measured_at),
        "FactoryTimestamp": libre_timestamp(measured_at),
        "MeasurementColor": 1,
        "isHigh": False,
        "isLow": False,
    }
    data.update(overrides)

    return data
