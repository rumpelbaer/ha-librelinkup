from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from custom_components.librelinkup.binary_sensor import (
    LibreLinkUpDataStaleSensor,
    LibreLinkUpHighSensor,
    LibreLinkUpLowSensor,
)
from custom_components.librelinkup.sensor import (
    LibreLinkUpGlucoseSensor,
    LibreLinkUpLastReadingSensor,
    LibreLinkUpReadingAgeSensor,
    LibreLinkUpTrendSensor,
)
from custom_components.librelinkup.utils import mg_dl_to_mmol_l


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.last_update_success = True

    def async_add_listener(self, update_callback, context=None):
        return lambda: None


def make_entry():
    entry = MagicMock()
    entry.entry_id = "test-entry"
    return entry


def test_glucose_sensor() -> None:
    coordinator = FakeCoordinator(
        {
            "Value": 13.7,
            "ValueInMgPerDl": 247,
            "TrendArrow": 3,
            "Timestamp": "9/14/2026 1:12:35 PM",
            "FactoryTimestamp": "9/14/2026 11:12:35 AM",
            "MeasurementColor": 2,
            "isHigh": False,
            "isLow": False,
        }
    )

    sensor = LibreLinkUpGlucoseSensor(coordinator, make_entry())

    assert sensor.native_value == 13.7
    assert sensor.extra_state_attributes["glucose_mmol_l"] == 13.7
    assert sensor.extra_state_attributes["glucose_mg_dl"] == 247


def test_glucose_sensor_converts_from_mg_dl_on_mmol_account() -> None:
    """An mmol/L account reports Value in mmol/L; the state must still come
    from ValueInMgPerDl."""
    coordinator = FakeCoordinator(
        {
            "Value": 13.7,
            "ValueInMgPerDl": 247,
            "GlucoseUnits": 0,
        }
    )

    sensor = LibreLinkUpGlucoseSensor(coordinator, make_entry())

    assert sensor.native_value == 13.7
    assert sensor.native_unit_of_measurement == "mmol/L"
    assert sensor.extra_state_attributes["glucose_mmol_l"] == 13.7
    assert sensor.extra_state_attributes["glucose_mg_dl"] == 247


def test_glucose_sensor_converts_from_mg_dl_on_mg_dl_account() -> None:
    """Regression test for the mg/dL account case.

    A mg/dL account reports Value in mg/dL. Using it directly would publish
    247 mmol/L, which would never trigger a low alert and would permanently
    read as very high.
    """
    coordinator = FakeCoordinator(
        {
            "Value": 247,
            "ValueInMgPerDl": 247,
        }
    )

    sensor = LibreLinkUpGlucoseSensor(coordinator, make_entry())

    assert sensor.native_value == 13.7
    assert sensor.native_value != 247
    assert sensor.native_unit_of_measurement == "mmol/L"
    assert sensor.extra_state_attributes["glucose_mmol_l"] == 13.7


def test_glucose_sensor_native_state_ignores_value_field() -> None:
    """Even a wildly inconsistent Value must not influence the state."""
    coordinator = FakeCoordinator(
        {
            "Value": 999,
            "ValueInMgPerDl": 115,
        }
    )

    sensor = LibreLinkUpGlucoseSensor(coordinator, make_entry())

    assert sensor.native_value == 6.4
    assert sensor.extra_state_attributes["glucose_mmol_l"] == 6.4


@pytest.mark.parametrize("raw", [115, 115.0, "115"])
def test_glucose_sensor_accepts_numeric_types(raw) -> None:
    coordinator = FakeCoordinator({"ValueInMgPerDl": raw})
    sensor = LibreLinkUpGlucoseSensor(coordinator, make_entry())

    assert sensor.native_value == 6.4


@pytest.mark.parametrize(
    "data",
    [
        {"ValueInMgPerDl": None},
        {"ValueInMgPerDl": "invalid"},
        {"ValueInMgPerDl": ""},
        {"ValueInMgPerDl": True},
        {"ValueInMgPerDl": [247]},
        {"Value": 13.7},
        {},
    ],
)
def test_glucose_sensor_returns_none_for_invalid_data(data) -> None:
    coordinator = FakeCoordinator(data)
    sensor = LibreLinkUpGlucoseSensor(coordinator, make_entry())

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["glucose_mmol_l"] is None


@pytest.mark.parametrize(
    ("mg_dl", "expected"),
    [
        (247, 13.7),
        (115, 6.4),
        (72, 4.0),
        (54, 3.0),
        (0, 0.0),
        (None, None),
        ("invalid", None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_mg_dl_to_mmol_l(mg_dl, expected) -> None:
    assert mg_dl_to_mmol_l(mg_dl) == expected


def test_trend_sensor() -> None:
    coordinator = FakeCoordinator({"TrendArrow": 3})
    sensor = LibreLinkUpTrendSensor(coordinator, make_entry())

    assert sensor.native_value == "stable"
    assert sensor.extra_state_attributes["trend_code"] == 3
    assert sensor.extra_state_attributes["trend_arrow"] == "→"


def test_low_and_high_sensors() -> None:
    coordinator = FakeCoordinator(
        {
            "isLow": True,
            "isHigh": False,
        }
    )

    low = LibreLinkUpLowSensor(coordinator, make_entry())
    high = LibreLinkUpHighSensor(coordinator, make_entry())

    assert low.is_on is True
    assert high.is_on is False


def test_last_reading_sensor() -> None:
    coordinator = FakeCoordinator(
        {
            "FactoryTimestamp": "9/14/2026 11:12:35 AM",
        }
    )

    sensor = LibreLinkUpLastReadingSensor(coordinator, make_entry())

    assert sensor.native_value == datetime(
        2026,
        9,
        14,
        11,
        12,
        35,
        tzinfo=UTC,
    )


def test_reading_age(monkeypatch) -> None:
    coordinator = FakeCoordinator(
        {
            "FactoryTimestamp": "9/14/2026 11:12:35 AM",
        }
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(
                2026,
                9,
                14,
                11,
                15,
                35,
                tzinfo=UTC,
            )

    monkeypatch.setattr(
        "custom_components.librelinkup.sensor.datetime",
        FixedDateTime,
    )

    sensor = LibreLinkUpReadingAgeSensor(coordinator, make_entry())

    assert sensor.native_value == 3


def test_data_stale(monkeypatch) -> None:
    coordinator = FakeCoordinator(
        {
            "FactoryTimestamp": "9/14/2026 11:12:35 AM",
        }
    )

    class FreshDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(
                2026,
                9,
                14,
                11,
                15,
                35,
                tzinfo=UTC,
            )

    monkeypatch.setattr(
        "custom_components.librelinkup.binary_sensor.datetime",
        FreshDateTime,
    )

    sensor = LibreLinkUpDataStaleSensor(coordinator, make_entry())
    assert sensor.is_on is False

    class StaleDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(
                2026,
                9,
                14,
                11,
                20,
                35,
                tzinfo=UTC,
            )

    monkeypatch.setattr(
        "custom_components.librelinkup.binary_sensor.datetime",
        StaleDateTime,
    )

    assert sensor.is_on is True
