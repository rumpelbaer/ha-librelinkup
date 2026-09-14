from __future__ import annotations

from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PATIENT_ID, CONF_PATIENT_NAME, DEFAULT_PATIENT_NAME, DOMAIN
from .coordinator import LibreLinkUpAccountCoordinator


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build the device every entity of this config entry belongs to.

    The device is identified by the entry ID, so renaming the patient in
    LibreLinkUp never re-creates the device or its entities. The patient name
    is display only, and falls back to a generic name for entries that predate
    it -- never to a connection looked up from the API, which could silently
    attach the entry to the wrong person, and never to the patient ID, which
    would put a health identifier into the device name.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_PATIENT_NAME) or DEFAULT_PATIENT_NAME,
        manufacturer="Abbott",
        model="FreeStyle LibreLinkUp",
    )


class LibreLinkUpPatientEntity(CoordinatorEntity[LibreLinkUpAccountCoordinator]):
    """One entity of one configured person, on the account's shared coordinator.

    Sensors and binary sensors differ only in what they read out of a
    measurement. Everything that decides *which* person an entity speaks for --
    its device, its unique ID, the measurement it may look at and whether that
    measurement may be served at all -- is the same for both and therefore
    lives here exactly once.
    """

    _attr_has_entity_name = True

    # Suffix of this entity's unique ID, appended to the config entry ID.
    #
    # Deliberately not spelled "_attr_key": "_attr_" is Home Assistant's
    # namespace for values backing an entity property, and Home Assistant has
    # no "key" property -- the prefix would suggest a meaning it does not have.
    #
    # Every one of these strings is part of an existing installation's entity
    # registry. Changing one orphans that entity and loses its history, so they
    # are pinned by test_entities.test_unique_id_suffixes_are_frozen.
    _entity_key: str

    def __init__(
        self,
        coordinator: LibreLinkUpAccountCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._patient_id: str = entry.data[CONF_PATIENT_ID]
        self._attr_device_info = build_device_info(entry)
        self._attr_unique_id = f"{entry.entry_id}_{self._entity_key}"

    @property
    def _measurement(self) -> dict:
        """This entry's patient only -- never the whole account snapshot."""
        return self.coordinator.measurement_for(self._patient_id) or {}

    @property
    def _measured_at(self) -> datetime | None:
        """When this patient's current reading was taken.

        Taken from the coordinator, which derived it once when the reading
        arrived. Parsing FactoryTimestamp again here would recompute -- on every
        state read, per entity -- a value the freshness rules already run on.
        """
        return self.coordinator.measured_at_for(self._patient_id)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.is_patient_available(
            self._patient_id
        )
