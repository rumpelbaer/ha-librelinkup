from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfBloodGlucoseConcentration, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LibreLinkUpCoordinator
from .utils import parse_libre_timestamp


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
    coordinator: LibreLinkUpCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            LibreLinkUpGlucoseSensor(coordinator, entry),
            LibreLinkUpTrendSensor(coordinator, entry),
            LibreLinkUpLastReadingSensor(coordinator, entry),
            LibreLinkUpReadingAgeSensor(coordinator, entry),
        ]
    )


class LibreLinkUpSensorBase(
    CoordinatorEntity[LibreLinkUpCoordinator],
    SensorEntity,
):
    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LibreLinkUp",
            manufacturer="Abbott",
            model="FreeStyle LibreLinkUp",
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
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_glucose"

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.get("Value")
        return float(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data

        return {
            "glucose_mmol_l": data.get("Value"),
            "glucose_mg_dl": data.get("ValueInMgPerDl"),
            "trend_arrow": data.get("TrendArrow"),
            "timestamp": data.get("Timestamp"),
            "factory_timestamp": data.get("FactoryTimestamp"),
            "measurement_color": data.get("MeasurementColor"),
            "is_high": data.get("isHigh"),
            "is_low": data.get("isLow"),
        }


class LibreLinkUpTrendSensor(LibreLinkUpSensorBase):
    _attr_name = "Trend"

    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_trend"

    @property
    def native_value(self) -> str | None:
        code = self.coordinator.data.get("TrendArrow")
        trend = TREND_MAP.get(code)
        return trend[0] if trend else None

    @property
    def extra_state_attributes(self) -> dict:
        code = self.coordinator.data.get("TrendArrow")
        trend = TREND_MAP.get(code)

        return {
            "trend_code": code,
            "trend_arrow": trend[1] if trend else None,
        }


class LibreLinkUpLastReadingSensor(LibreLinkUpSensorBase):
    _attr_name = "Last Reading"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_reading"

    @property
    def native_value(self) -> datetime | None:
        return parse_libre_timestamp(
            self.coordinator.data.get("FactoryTimestamp")
        )


class LibreLinkUpReadingAgeSensor(LibreLinkUpSensorBase):
    _attr_name = "Reading Age"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_reading_age"

    @property
    def native_value(self) -> int | None:
        measured_at = parse_libre_timestamp(
            self.coordinator.data.get("FactoryTimestamp")
        )

        if measured_at is None:
            return None

        age = datetime.now(UTC) - measured_at
        return max(0, round(age.total_seconds() / 60))
