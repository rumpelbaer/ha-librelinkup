from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfBloodGlucoseConcentration
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LibreLinkUpCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LibreLinkUpCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LibreLinkUpGlucoseSensor(coordinator, entry)])


class LibreLinkUpGlucoseSensor(
    CoordinatorEntity[LibreLinkUpCoordinator],
    SensorEntity,
):
    _attr_name = "LibreLinkUp Glucose"
    _attr_device_class = SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION
    _attr_native_unit_of_measurement = UnitOfBloodGlucoseConcentration.MILLIMOLE_PER_LITER

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
