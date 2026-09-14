from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LibreLinkUpApi
from .const import CONF_PATIENT_ID, DOMAIN, PLATFORMS
from .coordinator import LibreLinkUpAccountCoordinator


@dataclass
class AccountRuntime:
    """Everything shared by the config entries of one LibreLinkUp account."""

    api: LibreLinkUpApi
    coordinator: LibreLinkUpAccountCoordinator
    password: str
    entry_ids: set[str] = field(default_factory=set)


def account_key(entry: ConfigEntry) -> str:
    """Runtime-only key for an account.

    The normalized e-mail, never the password. This lives in hass.data and is
    never persisted or logged.
    """
    return entry.data[CONF_EMAIL].strip().lower()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not entry.data.get(CONF_PATIENT_ID):
        # Retrying cannot fix this, and guessing a connection could attach the
        # entry to the wrong person, so fail permanently and let the user
        # re-add the entry.
        raise ConfigEntryError(
            "This LibreLinkUp entry has no person selected. "
            "Remove it and set it up again."
        )

    accounts: dict[str, AccountRuntime] = hass.data.setdefault(DOMAIN, {})
    key = account_key(entry)
    runtime = accounts.get(key)

    if runtime is None:
        api = LibreLinkUpApi(
            async_get_clientsession(hass),
            key,
            entry.data[CONF_PASSWORD],
        )
        runtime = AccountRuntime(
            api=api,
            coordinator=LibreLinkUpAccountCoordinator(hass, api),
            password=entry.data[CONF_PASSWORD],
        )
        accounts[key] = runtime
    elif runtime.password != entry.data[CONF_PASSWORD]:
        # An entry being set up carries the most recently confirmed password
        # (a reauth reloads its entry), so it wins over what the shared client
        # was started with. The old token is dropped with it.
        runtime.password = entry.data[CONF_PASSWORD]
        runtime.api.update_password(entry.data[CONF_PASSWORD])

    runtime.entry_ids.add(entry.entry_id)
    _set_reauth_handler(hass, runtime)

    # Only the first entry of an account triggers the initial poll; the others
    # join a coordinator that already holds a snapshot.
    if runtime.coordinator.data is None:
        await runtime.coordinator.async_refresh()

        if not runtime.coordinator.last_update_success:
            failure = runtime.coordinator.last_exception
            await _async_release(hass, key, entry.entry_id)

            if isinstance(failure, ConfigEntryAuthFailed):
                raise ConfigEntryAuthFailed(
                    "LibreLinkUp authentication failed"
                ) from None

            raise ConfigEntryNotReady(
                "LibreLinkUp initial data update failed"
            ) from None

    entry.runtime_data = runtime.coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await _async_release(hass, account_key(entry), entry.entry_id)

    return unload_ok


async def _async_release(hass: HomeAssistant, key: str, entry_id: str) -> None:
    """Drop one entry from an account, tearing the account down when empty."""
    accounts: dict[str, AccountRuntime] = hass.data.get(DOMAIN, {})
    runtime = accounts.get(key)

    if runtime is None:
        return

    runtime.entry_ids.discard(entry_id)

    if runtime.entry_ids:
        _set_reauth_handler(hass, runtime)
        return

    accounts.pop(key, None)
    runtime.coordinator.async_request_reauth = None
    await runtime.coordinator.async_shutdown()


@callback
def _set_reauth_handler(hass: HomeAssistant, runtime: AccountRuntime) -> None:
    """Point account-wide auth failures at a single entry.

    Home Assistant starts reauth per config entry. Asking every patient's entry
    would show the user one dialog per person for a single wrong password, so
    only the lowest entry ID is asked; the flow then propagates the new
    password to the account's other entries.
    """

    @callback
    def request_reauth() -> None:
        if not runtime.entry_ids:
            return

        entry_id = min(runtime.entry_ids)
        entry = hass.config_entries.async_get_entry(entry_id)

        if entry is not None:
            entry.async_start_reauth(hass)

    runtime.coordinator.async_request_reauth = request_reauth
