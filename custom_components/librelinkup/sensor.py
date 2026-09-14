from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfBloodGlucoseConcentration, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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
        data = self._measurement
        mg_dl = data.get("ValueInMgPerDl")

        return {
            "glucose_mmol_l": mg_dl_to_mmol_l(mg_dl),
            "glucose_mg_dl": mg_dl,
            "trend_arrow": data.get("TrendArrow"),
            "timestamp": data.get("Timestamp"),
            "factory_timestamp": data.get("FactoryTimestamp"),
            "measurement_color": data.get("MeasurementColor"),
            "is_high": data.get("isHigh"),
            "is_low": data.get("isLow"),
        }


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
        code = self._measurement.get("TrendArrow")
        trend = TREND_MAP.get(code)
        return trend[0] if trend else None

    @property
    def extra_state_attributes(self) -> dict:
        code = self._measurement.get("TrendArrow")
        trend = TREND_MAP.get(code)

        return {
            "trend_code": code,
            "trend_arrow": trend[1] if trend else None,
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
    _attr_state_class = SensorStateClass.MEASUREMENT

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

        age = datetime.now(UTC) - measured_at
        return max(0, round(age.total_seconds() / 60))
