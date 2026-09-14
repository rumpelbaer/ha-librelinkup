from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import LibreLinkUpAccountCoordinator
from .entity import LibreLinkUpPatientEntity


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


class LibreLinkUpDataStaleSensor(LibreLinkUpPatientEntity, BinarySensorEntity):
    _entity_key = "data_stale"
    _attr_name = "Data Stale"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        measured_at = self._measured_at

        if measured_at is None:
            return True

        return dt_util.utcnow() - measured_at > STALE_AFTER


class LibreLinkUpLowSensor(LibreLinkUpPatientEntity, BinarySensorEntity):
    _entity_key = "low"
    _attr_name = "Low"

    @property
    def is_on(self) -> bool:
        return bool(self._measurement.get("isLow"))


class LibreLinkUpHighSensor(LibreLinkUpPatientEntity, BinarySensorEntity):
    _entity_key = "high"
    _attr_name = "High"

    @property
    def is_on(self) -> bool:
        return bool(self._measurement.get("isHigh"))
