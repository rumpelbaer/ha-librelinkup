from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite


MG_DL_PER_MMOL_L = 18.0182


def parse_libre_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.strptime(
            value,
            "%m/%d/%Y %I:%M:%S %p",
        ).replace(tzinfo=UTC)
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
