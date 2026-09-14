from __future__ import annotations

import hashlib

import voluptuous as vol
from aiohttp import ClientResponseError

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import LibreLinkUpApi, LibreLinkUpAuthenticationError
from .const import CONF_PATIENT_ID, CONF_PATIENT_NAME, DOMAIN


PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(
        type=TextSelectorType.PASSWORD,
        autocomplete="current-password",
    )
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.EMAIL,
                autocomplete="email",
            )
        ),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)


class LibreLinkUpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._pending_credentials: dict | None = None
        self._connections: dict[str, str] = {}
        self._reauth_entry = None

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
            except LibreLinkUpAuthenticationError:
                errors["base"] = "invalid_auth"
            except ClientResponseError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                if not connections:
                    errors["base"] = "no_connections"
                elif len(connections) == 1:
                    return await self._async_create_entry(
                        email,
                        password,
                        connections[0],
                    )
                else:
                    self._pending_credentials = {
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                    }
                    self._connections = {
                        connection["patientId"]: self._connection_name(connection)
                        for connection in connections
                    }
                    return await self.async_step_connection()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self,
        entry_data: dict,
    ) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict | None = None,
    ) -> FlowResult:
        errors: dict[str, str] = {}

        assert self._reauth_entry is not None

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            email = self._reauth_entry.data[CONF_EMAIL]

            api = LibreLinkUpApi(
                async_get_clientsession(self.hass),
                email,
                password,
            )

            try:
                await api.async_login()
                connections = await api.async_get_connections()
            except LibreLinkUpAuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                patient_id = self._reauth_entry.data.get(CONF_PATIENT_ID)

                if patient_id and not any(
                    connection.get("patientId") == patient_id
                    for connection in connections
                ):
                    errors["base"] = "no_connections"
                else:
                    new_data = {
                        **self._reauth_entry.data,
                        CONF_PASSWORD: password,
                    }

                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data=new_data,
                    )

                    await self.hass.config_entries.async_reload(
                        self._reauth_entry.entry_id
                    )

                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_connection(
        self,
        user_input: dict | None = None,
    ) -> FlowResult:
        if user_input is not None:
            patient_id = user_input[CONF_PATIENT_ID]

            connection = {
                "patientId": patient_id,
                "firstName": self._connections[patient_id],
                "lastName": "",
            }

            assert self._pending_credentials is not None

            return await self._async_create_entry(
                self._pending_credentials[CONF_EMAIL],
                self._pending_credentials[CONF_PASSWORD],
                connection,
                patient_name=self._connections[patient_id],
            )

        return self.async_show_form(
            step_id="connection",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PATIENT_ID): vol.In(self._connections),
                }
            ),
        )

    async def _async_create_entry(
        self,
        email: str,
        password: str,
        connection: dict,
        patient_name: str | None = None,
    ) -> FlowResult:
        patient_id = connection["patientId"]

        unique_id = hashlib.sha256(
            f"{email}:{patient_id}".encode("utf-8")
        ).hexdigest()

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        name = patient_name or self._connection_name(connection)

        return self.async_create_entry(
            title=f"LibreLinkUp - {name}",
            data={
                CONF_EMAIL: email,
                CONF_PASSWORD: password,
                CONF_PATIENT_ID: patient_id,
                CONF_PATIENT_NAME: name,
            },
        )

    @staticmethod
    def _connection_name(connection: dict) -> str:
        name = " ".join(
            part
            for part in (
                connection.get("firstName"),
                connection.get("lastName"),
            )
            if part
        ).strip()

        return name or connection.get("patientId", "LibreLinkUp")
