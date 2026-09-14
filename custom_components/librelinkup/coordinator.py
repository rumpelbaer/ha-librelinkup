from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LibreLinkUpApi, LibreLinkUpAuthenticationError
from .const import CONF_PATIENT_ID

_LOGGER = logging.getLogger(__name__)


class LibreLinkUpCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name="LibreLinkUp",
            update_interval=timedelta(seconds=60),
        )

        session = async_get_clientsession(hass)

        self.api = LibreLinkUpApi(
            session,
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
        )

        # Required by async_setup_entry, which rejects entries without it.
        # Never derived from the API: picking a connection here could silently
        # attach the entry to a different person.
        self.patient_id: str = entry.data[CONF_PATIENT_ID]

    async def _async_update_data(self) -> dict:
        try:
            measurement = await self.api.async_get_glucose_measurement(
                self.patient_id
            )

            if not measurement:
                raise RuntimeError(
                    "LibreLinkUp returned no glucose measurement"
                )

            return measurement

        except LibreLinkUpAuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "LibreLinkUp authentication failed"
            ) from err

        except Exception as err:
            if self.data:
                _LOGGER.warning(
                    "LibreLinkUp update failed (%s); keeping the last valid measurement",
                    type(err).__name__,
                )
                return self.data

            raise UpdateFailed(
                "LibreLinkUp initial data update failed"
            ) from err
