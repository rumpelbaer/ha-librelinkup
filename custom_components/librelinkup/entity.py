from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_PATIENT_NAME, DEFAULT_PATIENT_NAME, DOMAIN


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
