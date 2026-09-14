from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PATIENT_ID
from .coordinator import LibreLinkUpAccountCoordinator
from .entity import build_device_info
from .utils import parse_libre_timestamp


STALE_AFTER = timedelta(minutes=5)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LibreLinkUpAccountCoordinator = entry.runtime_data

    async_add_entities(
        [
            LibreLinkUpDataStaleSensor(coordinator, entry),
            LibreLinkUpLowSensor(coordinator, entry),
            LibreLinkUpHighSensor(coordinator, entry),
        ]
    )


class LibreLinkUpBinarySensorBase(
    CoordinatorEntity[LibreLinkUpAccountCoordinator],
    BinarySensorEntity,
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


class LibreLinkUpDataStaleSensor(LibreLinkUpBinarySensorBase):
    _attr_name = "Data Stale"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_data_stale"

    @property
    def is_on(self) -> bool:
        measured_at = parse_libre_timestamp(
            self._measurement.get("FactoryTimestamp")
        )

        if measured_at is None:
            return True

        return datetime.now(UTC) - measured_at > STALE_AFTER


class LibreLinkUpLowSensor(LibreLinkUpBinarySensorBase):
    _attr_name = "Low"

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_low"

    @property
    def is_on(self) -> bool:
        return bool(self._measurement.get("isLow"))


class LibreLinkUpHighSensor(LibreLinkUpBinarySensorBase):
    _attr_name = "High"

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_high"

    @property
    def is_on(self) -> bool:
        return bool(self._measurement.get("isHigh"))
