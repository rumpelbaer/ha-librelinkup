from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfBloodGlucoseConcentration
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LibreLinkUpCoordinator


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
        ]
    )


class LibreLinkUpGlucoseSensor(
    CoordinatorEntity[LibreLinkUpCoordinator],
    SensorEntity,
):
    _attr_name = "LibreLinkUp Glucose"
    _attr_device_class = SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION
    _attr_native_unit_of_measurement = UnitOfBloodGlucoseConcentration.MILLIMOLE_PER_LITER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
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


class LibreLinkUpTrendSensor(
    CoordinatorEntity[LibreLinkUpCoordinator],
    SensorEntity,
):
    _attr_name = "LibreLinkUp Trend"

    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
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
