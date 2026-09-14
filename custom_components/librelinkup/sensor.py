from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfBloodGlucoseConcentration, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_PATIENT_ID
from .coordinator import LibreLinkUpAccountCoordinator
from .entity import build_device_info
from .utils import mg_dl_to_mmol_l, parse_libre_timestamp


TREND_MAP = {
    0: ("not_determined", None),
    1: ("falling_rapidly", "↓↓"),
    2: ("falling", "↓"),
    3: ("stable", "→"),
    4: ("rising", "↑"),
    5: ("rising_rapidly", "↑↑"),
}


def trend_code(value: object) -> int | None:
    """Coerce a TrendArrow into a known trend code, or None.

    bool is rejected explicitly: it is a subclass of int, so a TrendArrow of
    ``true`` would otherwise be read as code 1 and displayed as "falling
    rapidly" -- a made-up trend on a health entity. Numeric strings are accepted
    because the glucose value is accepted in that form too.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None

    try:
        number = float(value)
    except ValueError:
        return None

    if not number.is_integer():
        return None

    code = int(number)

    return code if code in TREND_MAP else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LibreLinkUpAccountCoordinator = entry.runtime_data

    async_add_entities(
        [
            LibreLinkUpGlucoseSensor(coordinator, entry),
            LibreLinkUpTrendSensor(coordinator, entry),
            LibreLinkUpLastReadingSensor(coordinator, entry),
            LibreLinkUpReadingAgeSensor(coordinator, entry),
        ]
    )


class LibreLinkUpSensorBase(
    CoordinatorEntity[LibreLinkUpAccountCoordinator],
    SensorEntity,
):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._patient_id: str = entry.data[CONF_PATIENT_ID]
        self._attr_device_info = build_device_info(entry)

    @property
    def _measurement(self) -> dict:
        """This entry's patient only -- never the whole account snapshot."""
        return self.coordinator.measurement_for(self._patient_id) or {}

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.is_patient_available(
            self._patient_id
        )


class LibreLinkUpGlucoseSensor(LibreLinkUpSensorBase):
    _attr_name = "Glucose"
    _attr_device_class = SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION
    _attr_native_unit_of_measurement = (
        UnitOfBloodGlucoseConcentration.MILLIMOLE_PER_LITER
    )
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_glucose"

    @property
    def native_value(self) -> float | None:
        # "Value" follows the unit configured in the LibreLinkUp account and is
        # therefore ambiguous; "ValueInMgPerDl" is the only unit-explicit field.
        return mg_dl_to_mmol_l(self._measurement.get("ValueInMgPerDl"))

    @property
    def extra_state_attributes(self) -> dict:
        """The raw reading, and nothing that is already an entity.

        Every attribute is written to the recorder on each new reading, so the
        glucose history would otherwise be stored several times over: the
        converted value duplicates the state, the trend, the timestamp and the
        low/high flags duplicate their own entities.
        """
        return {"glucose_mg_dl": self._measurement.get("ValueInMgPerDl")}


class LibreLinkUpTrendSensor(LibreLinkUpSensorBase):
    _attr_name = "Trend"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["not_determined", "falling_rapidly", "falling", "stable", "rising", "rising_rapidly"]

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_trend"

    @property
    def native_value(self) -> str | None:
        code = trend_code(self._measurement.get("TrendArrow"))
        return TREND_MAP[code][0] if code is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        code = trend_code(self._measurement.get("TrendArrow"))

        return {
            "trend_code": code,
            "trend_arrow": TREND_MAP[code][1] if code is not None else None,
        }


class LibreLinkUpLastReadingSensor(LibreLinkUpSensorBase):
    _attr_name = "Last Reading"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_reading"

    @property
    def native_value(self) -> datetime | None:
        return parse_libre_timestamp(
            self._measurement.get("FactoryTimestamp")
        )


class LibreLinkUpReadingAgeSensor(LibreLinkUpSensorBase):
    _attr_name = "Reading Age"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    # Its value changes with every poll, so enabling it by default would write a
    # recorder row per minute per patient forever. Users who want it can turn it
    # on per entity. No state class either: long-term statistics of "how old is
    # the reading" carry no information worth keeping.
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_reading_age"

    @property
    def native_value(self) -> int | None:
        measured_at = parse_libre_timestamp(
            self._measurement.get("FactoryTimestamp")
        )

        if measured_at is None:
            return None

        age = dt_util.utcnow() - measured_at
        return max(0, round(age.total_seconds() / 60))
