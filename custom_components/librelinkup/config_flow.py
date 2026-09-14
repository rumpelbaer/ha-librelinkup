from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from aiohttp import ClientError

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    LibreLinkUpAccountStateError,
    LibreLinkUpApi,
    LibreLinkUpAuthenticationError,
    LibreLinkUpAuthorizationError,
    LibreLinkUpRegionError,
    LibreLinkUpResponseError,
)
from .const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DEFAULT_PATIENT_NAME,
    DOMAIN,
)
from .runtime import account_key

_LOGGER = logging.getLogger(__name__)

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

# States in which reloading an entry makes sense. A disabled entry is never
# reloaded: it would turn a successful password change into an error dialog.
RELOADABLE_STATES = (
    ConfigEntryState.LOADED,
    ConfigEntryState.SETUP_RETRY,
    ConfigEntryState.SETUP_ERROR,
)


class LibreLinkUpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._pending_credentials: dict | None = None
        self._connections: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Stored normalized, through the same function that later decides
            # which entries share an account.
            email = account_key(user_input[CONF_EMAIL])
            password = user_input[CONF_PASSWORD]

            connections, error = await self._async_load_connections(email, password)

            if error is not None:
                errors["base"] = error
            elif not connections:
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
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict | None = None,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            email = reauth_entry.data[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            connections, error = await self._async_load_connections(email, password)

            if error is not None:
                errors["base"] = error
            else:
                patient_id = reauth_entry.data.get(CONF_PATIENT_ID)

                if patient_id and not any(
                    connection.get("patientId") == patient_id
                    for connection in connections
                ):
                    errors["base"] = "share_revoked"
                else:
                    self._async_sync_account_password(email, password)
                    self._async_reload_account(email, reauth_entry)

                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_connection(
        self,
        user_input: dict | None = None,
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="connection",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_PATIENT_ID): vol.In(self._connections),
                    }
                ),
            )

        if self._pending_credentials is None:  # pragma: no cover
            return self.async_abort(reason="reauth_successful")

        patient_id = user_input[CONF_PATIENT_ID]
        name = self._connections[patient_id]

        return await self._async_create_entry(
            self._pending_credentials[CONF_EMAIL],
            self._pending_credentials[CONF_PASSWORD],
            {"patientId": patient_id},
            patient_name=name,
        )

    async def _async_load_connections(
        self, email: str, password: str
    ) -> tuple[list[dict], str | None]:
        """Verify credentials and list the people they give access to.

        Returns the connections and an error key. Every failure gets its own key:
        an unsupported region or a changed API format is not "could not
        connect", and the user cannot act on advice that describes the wrong
        problem.
        """
        api = LibreLinkUpApi(
            async_get_clientsession(self.hass),
            email,
            password,
        )

        try:
            await api.async_login()
            connections = await api.async_get_connections()
        except LibreLinkUpAuthenticationError:
            return [], "invalid_auth"
        except LibreLinkUpRegionError:
            return [], "unsupported_region"
        except LibreLinkUpAccountStateError:
            return [], "account_state"
        except LibreLinkUpResponseError:
            return [], "invalid_response"
        except (
            ClientError,
            LibreLinkUpAuthorizationError,
            TimeoutError,
        ):
            return [], "cannot_connect"
        except Exception:
            # Static message: the traceback must not be dressed up with the
            # e-mail, the token or the payload.
            _LOGGER.exception("Unexpected error while talking to LibreLinkUp")
            return [], "unknown"

        return connections, None

    @callback
    def _account_entries(self, email: str) -> list[ConfigEntry]:
        """Every config entry that belongs to this LibreLinkUp account.

        Account identity is decided by account_key and nowhere else, so the
        flow cannot drift apart from the runtime about which entries share a
        client -- the runtime keys hass.data by exactly this value.
        """
        target = account_key(email)

        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            # ".get" rather than account_key(entry): an entry that somehow
            # carries no address at all is skipped, as it was before, instead
            # of failing the flow of an unrelated account.
            if account_key(entry.data.get(CONF_EMAIL, "")) == target
        ]

    @callback
    def _async_sync_account_password(self, email: str, password: str) -> None:
        """Give every entry of this account the password just verified.

        Every entry of one account shares one client, so they must all carry the
        password that was confirmed last -- otherwise whichever entry loads first
        after a restart would decide which password the account runs on, and a
        stale one would keep being offered to Abbott.
        """
        for other in self._account_entries(email):
            if other.data.get(CONF_PASSWORD) == password:
                continue

            self.hass.config_entries.async_update_entry(
                other,
                data={**other.data, CONF_PASSWORD: password},
            )

    @callback
    def _async_reload_account(
        self, email: str, preferred: ConfigEntry | None = None
    ) -> None:
        """Reload one entry of the account so the shared client is rebuilt.

        One is enough: the entries share a runtime, and its setup adopts the new
        password and refreshes the account. The reauth entry is preferred, but if
        it is disabled or was never loaded, a sibling repairs the account instead
        of the flow failing.
        """
        candidates = sorted(
            self._account_entries(email),
            key=lambda entry: entry.entry_id,
        )

        if preferred is not None and preferred in candidates:
            candidates.remove(preferred)
            candidates.insert(0, preferred)

        for entry in candidates:
            if entry.disabled_by is not None or entry.state not in RELOADABLE_STATES:
                continue

            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return

    async def _async_create_entry(
        self,
        email: str,
        password: str,
        connection: dict,
        patient_name: str | None = None,
    ) -> ConfigFlowResult:
        patient_id = connection["patientId"]

        unique_id = hashlib.sha256(
            f"{email}:{patient_id}".encode("utf-8")
        ).hexdigest()

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        name = patient_name or self._connection_name(connection)

        # The password was just verified, so the account's existing entries have
        # to adopt it too -- otherwise adding a person after a password change
        # leaves the account with two different stored passwords.
        self._async_sync_account_password(email, password)

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
        """Display name of a connection, never its patient ID.

        A patient ID as the fallback would be persisted as the entry title and
        the device name, putting a health identifier in front of the user and
        into every screenshot. Entries stay distinguishable through their unique
        ID and device identifier instead.
        """
        name = " ".join(
            part
            for part in (
                connection.get("firstName"),
                connection.get("lastName"),
            )
            if isinstance(part, str) and part
        ).strip()

        return name or DEFAULT_PATIENT_NAME
