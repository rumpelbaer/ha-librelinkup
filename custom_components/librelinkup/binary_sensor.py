from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LibreLinkUpCoordinator

STALE_AFTER = timedelta(minutes=5)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: LibreLinkUpCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LibreLinkUpDataStaleSensor(coordinator, entry)])


class LibreLinkUpDataStaleSensor(CoordinatorEntity[LibreLinkUpCoordinator], BinarySensorEntity):
    _attr_name = "LibreLinkUp Data Stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: LibreLinkUpCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_data_stale"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="LibreLinkUp", manufacturer="Abbott", model="FreeStyle LibreLinkUp")

    @property
    def is_on(self) -> bool:
        timestamp = self.coordinator.data.get("FactoryTimestamp")
        if not timestamp:
            return True
        try:
            measured_at = datetime.strptime(timestamp, "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=UTC)
        except ValueError:
            return True
        return datetime.now(UTC) - measured_at > STALE_AFTER
