from __future__ import annotations

import re
from datetime import UTC, datetime
from math import isfinite


MG_DL_PER_MMOL_L = 18.0182

# LibreLinkUp reports "FactoryTimestamp" as "M/D/YYYY h:mm:ss AM/PM" in UTC.
# Parsed by hand instead of strptime("%p"), because %p resolves AM/PM through
# LC_TIME: under a German locale the meridiem list is empty and every timestamp
# would silently fail to parse.
LIBRE_TIMESTAMP_PATTERN = re.compile(
    r"(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+"
    r"(?P<meridiem>[AP])M",
    re.IGNORECASE,
)


def parse_libre_timestamp(value: str | None) -> datetime | None:
    """Parse a LibreLinkUp UTC timestamp, locale-independently.

    Returns ``None`` for anything that is not a well-formed timestamp, so that
    malformed API data never raises into the entity layer.
    """
    if not value:
        return None

    match = LIBRE_TIMESTAMP_PATTERN.fullmatch(value.strip())

    if match is None:
        return None

    hour = int(match["hour"])

    if not 1 <= hour <= 12:
        return None

    hour %= 12

    if match["meridiem"].upper() == "P":
        hour += 12

    try:
        return datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            hour,
            int(match["minute"]),
            int(match["second"]),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def mg_dl_to_mmol_l(value: object) -> float | None:
    """Convert a mg/dL glucose value to mmol/L.

    ``ValueInMgPerDl`` is the only unambiguously named glucose field in the
    LibreLinkUp response. ``Value`` follows the display unit configured in the
    LibreLinkUp account and must therefore never be used as the source for the
    native Home Assistant state.

    Malformed API data must not raise into the entity layer, so anything that
    is not a finite number returns ``None``.
    """
    if value is None or isinstance(value, bool):
        return None

    try:
        mg_dl = float(value)
    except (TypeError, ValueError):
        return None

    if not isfinite(mg_dl):
        return None

    return round(mg_dl / MG_DL_PER_MMOL_L, 1)
