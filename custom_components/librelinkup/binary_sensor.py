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
from .utils import parse_libre_timestamp


STALE_AFTER = timedelta(minutes=5)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LibreLinkUpCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            LibreLinkUpDataStaleSensor(coordinator, entry),
            LibreLinkUpLowSensor(coordinator, entry),
            LibreLinkUpHighSensor(coordinator, entry),
        ]
    )


class LibreLinkUpBinarySensorBase(
    CoordinatorEntity[LibreLinkUpCoordinator],
    BinarySensorEntity,
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


class LibreLinkUpDataStaleSensor(LibreLinkUpBinarySensorBase):
    _attr_name = "Data Stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_data_stale"

    @property
    def is_on(self) -> bool:
        measured_at = parse_libre_timestamp(
            self.coordinator.data.get("FactoryTimestamp")
        )

        if measured_at is None:
            return True

        return datetime.now(UTC) - measured_at > STALE_AFTER


class LibreLinkUpLowSensor(LibreLinkUpBinarySensorBase):
    _attr_name = "Low"

    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_low"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("isLow"))


class LibreLinkUpHighSensor(LibreLinkUpBinarySensorBase):
    _attr_name = "High"

    def __init__(
        self,
        coordinator: LibreLinkUpCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_high"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("isHigh"))
