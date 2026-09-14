from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LibreLinkUpApi
from .const import (
    CONF_PATIENT_ID,
    DOMAIN,
    ISSUE_ACCOUNT_STATE,
    ISSUE_LEGACY_ENTRY,
    ISSUE_SHARE_REVOKED,
    PLATFORMS,
)
from .coordinator import LibreLinkUpAccountCoordinator

ISSUE_KINDS = (ISSUE_LEGACY_ENTRY, ISSUE_SHARE_REVOKED, ISSUE_ACCOUNT_STATE)


@dataclass
class AccountRuntime:
    """Everything shared by the config entries of one LibreLinkUp account."""

    api: LibreLinkUpApi
    coordinator: LibreLinkUpAccountCoordinator
    password: str
    # entry_id -> patient_id of every loaded entry of this account. The patient
    # IDs are what the coordinator is allowed to keep data for.
    entries: dict[str, str] = field(default_factory=dict)
    # Entries of one account are set up concurrently; the lock keeps them from
    # each firing their own initial poll.
    setup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def patient_ids(self) -> frozenset[str]:
        return frozenset(self.entries.values())


def account_key(entry: ConfigEntry) -> str:
    """Runtime-only key for an account.

    The normalized e-mail, never the password. This lives in hass.data and is
    never persisted or logged.
    """
    return entry.data[CONF_EMAIL].strip().lower()


def _issue_id(kind: str, entry_id: str) -> str:
    """Repair issue ID for one config entry.

    Keyed by the config entry ID, never by a patient ID: repair issues are
    persisted in .storage and must not carry health identifiers.
    """
    return f"{kind}_{entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    patient_id = entry.data.get(CONF_PATIENT_ID)

    if not patient_id:
        # Retrying cannot fix this, and guessing a connection could attach the
        # entry to the wrong person, so fail permanently and let the user
        # re-add the entry.
        _async_create_issue(hass, entry, ISSUE_LEGACY_ENTRY)

        raise ConfigEntryError(
            "This LibreLinkUp entry has no person selected. "
            "Remove it and set it up again."
        )

    _async_delete_issue(hass, entry.entry_id, ISSUE_LEGACY_ENTRY)

    accounts: dict[str, AccountRuntime] = hass.data.setdefault(DOMAIN, {})
    key = account_key(entry)
    runtime = accounts.get(key)

    if runtime is None:
        api = LibreLinkUpApi(
            async_get_clientsession(hass),
            key,
            entry.data[CONF_PASSWORD],
        )
        coordinator = LibreLinkUpAccountCoordinator(hass, api)
        # The coordinator is not bound to a config entry, so Home Assistant does
        # not stop it on shutdown by itself.
        await coordinator.async_register_shutdown()
        runtime = AccountRuntime(
            api=api,
            coordinator=coordinator,
            password=entry.data[CONF_PASSWORD],
        )
        accounts[key] = runtime
    elif runtime.password != entry.data[CONF_PASSWORD]:
        # An entry being set up carries the most recently confirmed password
        # (both the reauth flow and adding a person reload or write every entry
        # of the account), so it wins over what the shared client was started
        # with. The old token is dropped with it.
        runtime.password = entry.data[CONF_PASSWORD]
        runtime.api.update_password(entry.data[CONF_PASSWORD])

    runtime.entries[entry.entry_id] = patient_id
    runtime.coordinator.async_set_configured_patients(runtime.patient_ids)
    _async_bind_runtime(hass, runtime)
    _async_apply_polling_preference(hass, runtime)

    async with runtime.setup_lock:
        coordinator = runtime.coordinator

        if coordinator.data is None or not coordinator.last_update_success:
            # Also covers the reload after a successful reauth: Home Assistant
            # stops scheduling refreshes once an update raised
            # ConfigEntryAuthFailed and expects the reload to restart the
            # coordinator -- which a shared coordinator survives, so the account
            # would never poll again without this refresh.
            await coordinator.async_refresh()

            if not coordinator.last_update_success:
                failure = coordinator.last_exception
                await _async_release(hass, key, entry.entry_id)

                if isinstance(failure, ConfigEntryAuthFailed):
                    raise ConfigEntryAuthFailed(
                        "LibreLinkUp authentication failed"
                    ) from None

                if isinstance(failure, ConfigEntryError):
                    # Raised by the coordinator with a static message; re-raised
                    # as is, with "from None" so its own cause -- which may
                    # render a request URL -- is dropped.
                    raise failure from None

                raise ConfigEntryNotReady(
                    "LibreLinkUp initial data update failed"
                ) from None

        elif coordinator.measurement_for(patient_id) is None:
            # A person added to an account that is already polling should not
            # wait out the remaining interval before their entities work. Asking
            # whether the snapshot already holds this patient instead of whether
            # the entry is new keeps this from costing a request per entry while
            # several are being set up at once: whoever polls first covers every
            # patient that was already registered.
            await coordinator.async_refresh()

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

    runtime.entries.pop(entry_id, None)

    for kind in ISSUE_KINDS:
        _async_delete_issue(hass, entry_id, kind)

    if runtime.entries:
        # Drops everything the coordinator still held for the removed patient.
        runtime.coordinator.async_set_configured_patients(runtime.patient_ids)
        _async_bind_runtime(hass, runtime)
        _async_apply_polling_preference(hass, runtime)
        return

    accounts.pop(key, None)
    runtime.coordinator.async_request_reauth = None
    runtime.coordinator.async_report_missing_patients = None
    runtime.coordinator.async_report_account_state = None
    await runtime.coordinator.async_shutdown()


@callback
def _async_apply_polling_preference(
    hass: HomeAssistant, runtime: AccountRuntime
) -> None:
    """Let the entries of an account decide whether it polls automatically.

    Home Assistant reloads a config entry when its "Enable polling for updates"
    option changes, so recomputing this on every setup and unload is enough.
    """
    enabled = False

    for entry_id in runtime.entries:
        entry = hass.config_entries.async_get_entry(entry_id)

        if entry is not None and not entry.pref_disable_polling:
            enabled = True
            break

    runtime.coordinator.async_set_polling_enabled(enabled)


@callback
def _async_bind_runtime(hass: HomeAssistant, runtime: AccountRuntime) -> None:
    """Wire the account's coordinator to reauth and to repair issues.

    Home Assistant starts reauth per config entry. Asking every patient's entry
    would show the user one dialog per person for a single wrong password, so
    only the lowest entry ID is asked; the flow then propagates the new password
    to the account's other entries.
    """
    coordinator = runtime.coordinator

    @callback
    def request_reauth() -> None:
        if not runtime.entries:
            return

        # Home Assistant deduplicates reauth flows per config entry, not per
        # account, so a flow that is already open for any entry of this account
        # -- including one whose entry has meanwhile been removed and replaced
        # as the chosen one -- has to be found here.
        for flow in hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            match_context={"source": SOURCE_REAUTH},
            include_uninitialized=True,
        ):
            if flow["context"].get("entry_id") in runtime.entries:
                return

        entry = hass.config_entries.async_get_entry(min(runtime.entries))

        if entry is not None:
            entry.async_start_reauth(hass)

    @callback
    def report_missing_patients(patient_ids: frozenset[str]) -> None:
        for entry_id, patient_id in runtime.entries.items():
            entry = hass.config_entries.async_get_entry(entry_id)

            if entry is None:
                continue

            if patient_id in patient_ids:
                _async_create_issue(hass, entry, ISSUE_SHARE_REVOKED)
            else:
                _async_delete_issue(hass, entry_id, ISSUE_SHARE_REVOKED)

    @callback
    def report_account_state(problem: bool) -> None:
        if not problem:
            for entry_id in runtime.entries:
                _async_delete_issue(hass, entry_id, ISSUE_ACCOUNT_STATE)
            return

        if not runtime.entries:
            return

        # One issue per account, on the same entry that would be asked to
        # reauth, so a three-patient account does not show three identical cards.
        entry = hass.config_entries.async_get_entry(min(runtime.entries))

        if entry is not None:
            _async_create_issue(hass, entry, ISSUE_ACCOUNT_STATE)

    coordinator.async_request_reauth = request_reauth
    coordinator.async_report_missing_patients = report_missing_patients
    coordinator.async_report_account_state = report_account_state


@callback
def _async_create_issue(hass: HomeAssistant, entry: ConfigEntry, kind: str) -> None:
    """Raise a repair issue for one config entry.

    The entry title is the only placeholder: it is a Home Assistant reference the
    user recognizes, unlike a patient ID, which must never be persisted here.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(kind, entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=kind,
        translation_placeholders={"entry_title": entry.title},
    )


@callback
def _async_delete_issue(hass: HomeAssistant, entry_id: str, kind: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(kind, entry_id))
