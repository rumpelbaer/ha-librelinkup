from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

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
