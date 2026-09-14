from __future__ import annotations

from datetime import UTC, datetime


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
