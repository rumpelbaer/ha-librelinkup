from __future__ import annotations

import voluptuous as vol
from aiohttp import ClientResponseError

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LibreLinkUpApi
from .const import DOMAIN


class LibreLinkUpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            password = user_input[CONF_PASSWORD]

            api = LibreLinkUpApi(
                async_get_clientsession(self.hass),
                email,
                password,
            )

            try:
                await api.async_login()
                connections = await api.async_get_connections()
            except ClientResponseError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                if not connections:
                    errors["base"] = "no_connections"
                else:
                    await self.async_set_unique_id(email)
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title="LibreLinkUp",
                        data={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: password,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
