from __future__ import annotations

import ast
import inspect
import locale
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
from custom_components.librelinkup import utils
from custom_components.librelinkup.utils import (
    mg_dl_to_mmol_l,
    parse_libre_timestamp,
)


PATIENT_ID = "test-patient"


class FakeCoordinator:
    """Stands in for the shared account coordinator, holding one patient."""

    def __init__(self, data: dict, patient_id: str = PATIENT_ID) -> None:
        self.data = {patient_id: data}
        self.last_update_success = True
        self._patient_id = patient_id

    def measurement_for(self, patient_id):
        return self.data.get(patient_id)

    def measured_at_for(self, patient_id):
        """Mirrors the real coordinator: derived once, when the reading lands."""
        measurement = self.data.get(patient_id) or {}

        return parse_libre_timestamp(measurement.get("FactoryTimestamp"))

    def is_patient_available(self, patient_id) -> bool:
        return patient_id in self.data

    def async_add_listener(self, update_callback, context=None):
        return lambda: None


def make_entry(patient_id: str = PATIENT_ID):
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.data = {"patient_id": patient_id}
    return entry


def test_unique_id_suffixes_are_frozen() -> None:
    """The seven unique IDs, spelled out as they exist in the wild.

    Every one of them is an entity registry key of every installation that has
    ever run this integration. A changed suffix does not fail anywhere at
    runtime: Home Assistant simply registers a new entity and the old one, with
    all of its history, is orphaned.

    Written out literally instead of being built from _entity_key, so that a
    refactor of the entity classes cannot quietly move the expectation along
    with the code it is supposed to pin down.
    """
    coordinator = FakeCoordinator({})
    entry = make_entry()

    entities = (
        LibreLinkUpGlucoseSensor(coordinator, entry),
        LibreLinkUpTrendSensor(coordinator, entry),
        LibreLinkUpLastReadingSensor(coordinator, entry),
        LibreLinkUpReadingAgeSensor(coordinator, entry),
        LibreLinkUpDataStaleSensor(coordinator, entry),
        LibreLinkUpLowSensor(coordinator, entry),
        LibreLinkUpHighSensor(coordinator, entry),
    )

    assert {
        type(entity).__name__: entity.unique_id for entity in entities
    } == {
        "LibreLinkUpGlucoseSensor": "test-entry_glucose",
        "LibreLinkUpTrendSensor": "test-entry_trend",
        "LibreLinkUpLastReadingSensor": "test-entry_last_reading",
        "LibreLinkUpReadingAgeSensor": "test-entry_reading_age",
        "LibreLinkUpDataStaleSensor": "test-entry_data_stale",
        "LibreLinkUpLowSensor": "test-entry_low",
        "LibreLinkUpHighSensor": "test-entry_high",
    }


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
    assert sensor.extra_state_attributes == {"glucose_mg_dl": 247}


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
    assert sensor.extra_state_attributes == {"glucose_mg_dl": 247}


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
    assert sensor.extra_state_attributes == {"glucose_mg_dl": 247}


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
    assert sensor.extra_state_attributes == {"glucose_mg_dl": 115}


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
    assert "glucose_mmol_l" not in sensor.extra_state_attributes


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


def test_reading_age(freezer) -> None:
    """Frozen through Home Assistant's own clock, not by patching datetime."""
    freezer.move_to("2026-09-14 11:15:35+00:00")

    coordinator = FakeCoordinator(
        {
            "FactoryTimestamp": "9/14/2026 11:12:35 AM",
        }
    )

    sensor = LibreLinkUpReadingAgeSensor(coordinator, make_entry())

    assert sensor.native_value == 3

    freezer.move_to("2026-09-14 12:12:35+00:00")
    assert sensor.native_value == 60


def test_reading_age_is_disabled_by_default() -> None:
    """It changes every minute, so it must not fill the recorder unasked."""
    sensor = LibreLinkUpReadingAgeSensor(FakeCoordinator({}), make_entry())

    assert sensor.entity_registry_enabled_default is False
    # Long term statistics of "how old is the reading" carry no information.
    assert sensor.state_class is None


def test_data_stale(freezer) -> None:
    freezer.move_to("2026-09-14 11:15:35+00:00")

    coordinator = FakeCoordinator(
        {
            "FactoryTimestamp": "9/14/2026 11:12:35 AM",
        }
    )

    sensor = LibreLinkUpDataStaleSensor(coordinator, make_entry())

    assert sensor.is_on is False

    freezer.move_to("2026-09-14 11:20:35+00:00")

    assert sensor.is_on is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Verified LibreLinkUp format: M/D/YYYY h:mm:ss AM/PM, always UTC.
        ("9/14/2026 11:12:35 AM", datetime(2026, 9, 14, 11, 12, 35, tzinfo=UTC)),
        ("9/14/2026 11:12:35 PM", datetime(2026, 9, 14, 23, 12, 35, tzinfo=UTC)),
        # Midnight and noon are the two cases a hand-rolled parser gets wrong.
        ("9/14/2026 12:05:00 AM", datetime(2026, 9, 14, 0, 5, 0, tzinfo=UTC)),
        ("9/14/2026 12:05:00 PM", datetime(2026, 9, 14, 12, 5, 0, tzinfo=UTC)),
        ("9/14/2026 12:59:59 PM", datetime(2026, 9, 14, 12, 59, 59, tzinfo=UTC)),
        ("1/1/2026 1:00:00 AM", datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)),
        ("12/31/2026 11:59:59 PM", datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)),
        # Zero-padded and lower-case variants must keep working.
        ("09/14/2026 09:12:35 am", datetime(2026, 9, 14, 9, 12, 35, tzinfo=UTC)),
    ],
)
def test_parse_libre_timestamp_valid(raw, expected) -> None:
    assert parse_libre_timestamp(raw) == expected


def test_parse_libre_timestamp_is_utc_aware() -> None:
    parsed = parse_libre_timestamp("9/14/2026 11:12:35 AM")

    assert parsed is not None
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "invalid",
        "9/14/2026 25:61:99 PM",  # hour/minute/second out of range
        "9/14/2026 13:12:35 PM",  # 13 is not a valid 12-hour clock hour
        "9/14/2026 00:12:35 AM",  # neither is 0
        "2/30/2026 11:12:35 AM",  # February 30th does not exist
        "9/14/2026 11:12:35",  # meridiem missing
        "9/14/2026 11:12 AM",  # seconds missing
        "9/14/26 11:12:35 AM",  # two-digit year
        "9/14/2026 11:12:35 XM",  # bogus meridiem
        "9/14/2026 11:12:35 AM extra",  # trailing garbage
    ],
)
def test_parse_libre_timestamp_invalid(raw) -> None:
    assert parse_libre_timestamp(raw) is None


def test_parse_libre_timestamp_does_not_call_strptime() -> None:
    """Guard against reintroducing the locale-dependent %p directive.

    Checked on the AST so that the explanatory comment mentioning %p does not
    make this pass or fail for the wrong reason.
    """
    tree = ast.parse(inspect.getsource(utils))

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "strptime" not in called


@pytest.fixture
def german_lc_time():
    """Temporarily switch LC_TIME to German, restoring it afterwards."""
    previous = locale.setlocale(locale.LC_TIME)

    for candidate in ("de_DE.UTF-8", "de_DE.utf8", "de_DE"):
        try:
            locale.setlocale(locale.LC_TIME, candidate)
        except locale.Error:
            continue
        break
    else:
        pytest.skip("No German locale available on this system")

    try:
        yield
    finally:
        locale.setlocale(locale.LC_TIME, previous)


def test_parse_libre_timestamp_is_locale_independent(german_lc_time) -> None:
    """Regression test for the %p/LC_TIME failure.

    Under a German LC_TIME the AM/PM list is empty, so strptime("%p") rejects
    every LibreLinkUp timestamp. That silently broke Last Reading, Reading Age
    and pinned Data Stale to "problem".
    """
    assert parse_libre_timestamp("9/14/2026 11:12:35 PM") == datetime(
        2026, 9, 14, 23, 12, 35, tzinfo=UTC
    )
    assert parse_libre_timestamp("9/14/2026 11:12:35 AM") == datetime(
        2026, 9, 14, 11, 12, 35, tzinfo=UTC
    )
    assert parse_libre_timestamp("9/14/2026 12:05:00 AM") == datetime(
        2026, 9, 14, 0, 5, 0, tzinfo=UTC
    )


def test_entities_parse_pm_timestamps(german_lc_time) -> None:
    """The entities must stay wired to the locale-independent parser."""
    coordinator = FakeCoordinator({"FactoryTimestamp": "9/14/2026 1:12:35 PM"})

    assert LibreLinkUpLastReadingSensor(coordinator, make_entry()).native_value == (
        datetime(2026, 9, 14, 13, 12, 35, tzinfo=UTC)
    )
    assert LibreLinkUpReadingAgeSensor(coordinator, make_entry()).native_value is not None
    assert LibreLinkUpDataStaleSensor(coordinator, make_entry()).is_on is True


def test_the_native_unit_stays_mmol_l() -> None:
    """Why the state is mmol/L although the API's own field is mg/dL.

    Reporting mg/dL natively and letting Home Assistant convert looks tidier, but
    Home Assistant's blood glucose converter uses a flat factor of 18.0 and does
    not round: an mmol/L user would get a state of 6.38888888888889 instead of
    6.4. Converting here with the factor used in diabetes care keeps the primary
    unit clean and costs a mg/dL user 0.2 mg/dL of display precision.
    """
    coordinator = FakeCoordinator({"ValueInMgPerDl": 115})
    sensor = LibreLinkUpGlucoseSensor(coordinator, make_entry())

    assert sensor.native_unit_of_measurement == "mmol/L"
    assert sensor.native_value == 6.4
    # The exact value the API sent stays available for anyone who needs it.
    assert sensor.extra_state_attributes["glucose_mg_dl"] == 115
