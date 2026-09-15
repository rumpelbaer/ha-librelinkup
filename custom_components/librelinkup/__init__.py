from __future__ import annotations

from typing import NoReturn

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
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
from .runtime import (
    ISSUE_KINDS,
    AccountRuntime,
    account_key,
    async_create_issue,
    async_delete_issue,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    patient_id = entry.data.get(CONF_PATIENT_ID)

    if not patient_id:
        # Retrying cannot fix this, and guessing a connection could attach the
        # entry to the wrong person, so fail permanently and let the user
        # re-add the entry.
        async_create_issue(hass, entry, ISSUE_LEGACY_ENTRY)

        raise ConfigEntryError(
            "This LibreLinkUp entry has no person selected. "
            "Remove it and set it up again."
        )

    async_delete_issue(hass, entry.entry_id, ISSUE_LEGACY_ENTRY)

    accounts: dict[str, AccountRuntime] = hass.data.setdefault(DOMAIN, {})
    key = account_key(entry)

    runtime = await _async_get_or_create_runtime(hass, accounts, key, entry)

    # Registering the patient before any refresh is what lets the poll below
    # cover this person at all -- the coordinator drops everything it is not
    # configured for.
    runtime.entries[entry.entry_id] = patient_id
    runtime.coordinator.async_set_configured_patients(runtime.patient_ids)
    _async_bind_runtime(hass, key, runtime)
    _async_apply_polling_preference(hass, runtime)

    await _async_initial_refresh(hass, runtime, key, entry, patient_id)

    # Only now: an entry whose initial refresh failed was released above and
    # must not be left pointing at the account's coordinator.
    entry.runtime_data = runtime.coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_get_or_create_runtime(
    hass: HomeAssistant,
    accounts: dict[str, AccountRuntime],
    key: str,
    entry: ConfigEntry,
) -> AccountRuntime:
    """The one runtime of this account, created when its first entry loads.

    Every entry of an account shares one client and one coordinator, so the
    second person of an account joins what the first one started rather than
    opening a second session to Abbott.
    """
    runtime = accounts.get(key)
    password = entry.data[CONF_PASSWORD]

    if runtime is None:
        api = LibreLinkUpApi(async_get_clientsession(hass), key, password)
        coordinator = LibreLinkUpAccountCoordinator(hass, api)
        # The coordinator is not bound to a config entry, so Home Assistant does
        # not stop it on shutdown by itself.
        await coordinator.async_register_shutdown()
        runtime = AccountRuntime(
            api=api,
            coordinator=coordinator,
            password=password,
        )
        accounts[key] = runtime

    elif runtime.password != password:
        # An entry being set up carries the most recently confirmed password
        # (both the reauth flow and adding a person reload or write every entry
        # of the account), so it wins over what the shared client was started
        # with. The old token is dropped with it.
        runtime.password = password
        runtime.api.update_password(password)

    return runtime


async def _async_initial_refresh(
    hass: HomeAssistant,
    runtime: AccountRuntime,
    key: str,
    entry: ConfigEntry,
    patient_id: str,
) -> None:
    """Give this entry data to build its entities from, polling if needed.

    Entries of one account are set up concurrently, so the lock keeps each of
    them from firing its own poll: whoever gets here first covers every patient
    registered by then.

    There are two quite different reasons to poll here, and only the first one
    can fail the setup -- the account having no usable data at all, as opposed
    to it polling happily for somebody else.
    """
    async with runtime.setup_lock:
        coordinator = runtime.coordinator

        if coordinator.data is None or not coordinator.last_update_success:
            # The account has nothing to serve. Also covers the reload after a
            # successful reauth: Home Assistant stops scheduling refreshes once
            # an update raised ConfigEntryAuthFailed and expects the reload to
            # restart the coordinator -- which a shared coordinator survives, so
            # the account would never poll again without this refresh.
            await coordinator.async_refresh()

            if not coordinator.last_update_success:
                failure = coordinator.last_exception
                # Released before raising, so a failed setup leaves neither the
                # entry nor its patient behind in the shared runtime.
                await _async_release(hass, key, entry.entry_id)

                _raise_setup_failure(hass, key, entry.entry_id, failure)

        elif coordinator.measurement_for(patient_id) is None:
            # A person added to an account that is already polling should not
            # wait out the remaining interval before their entities work. Asking
            # whether the snapshot already holds this patient instead of whether
            # the entry is new keeps this from costing a request per entry while
            # several are being set up at once: whoever polls first covers every
            # patient that was already registered.
            await coordinator.async_refresh()


def _raise_setup_failure(
    hass: HomeAssistant, key: str, entry_id: str, failure: Exception | None
) -> NoReturn:
    """Tell Home Assistant what to do about a failed initial update.

    The one place that decides between a re-authentication dialog, a permanent
    error and a retry. It raises rather than returning the exception so that
    these messages stay literal strings inside a raise, where test_security can
    still see them.

    "from None" throughout: the cause may render a request URL, and a
    ClientResponseError's repr() carries the bearer token.
    """
    if isinstance(failure, ConfigEntryAuthFailed):
        asked = _async_account_reauth_entry_id(hass, key)

        if asked is not None and asked != entry_id:
            # The account is already asking for its password, on one of its
            # other entries. Home Assistant starts a reauth flow for every entry
            # whose setup raises ConfigEntryAuthFailed and deduplicates those
            # per entry, so raising it here would put a second dialog for the
            # same wrong password in front of the user -- one per person on the
            # account. A plain error instead: answering the one dialog reloads
            # every entry of the account, which is what brings this one back.
            raise ConfigEntryError(
                "LibreLinkUp rejected the password for this account. "
                "Enter it again in the re-authentication dialog Home Assistant "
                "is already showing for this account."
            ) from None

        raise ConfigEntryAuthFailed("LibreLinkUp authentication failed") from None

    if isinstance(failure, ConfigEntryError):
        # Raised by the coordinator with a static message; re-raised as is.
        raise failure from None

    raise ConfigEntryNotReady("LibreLinkUp initial data update failed") from None


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
        async_delete_issue(hass, entry_id, kind)

    if runtime.entries:
        # Drops everything the coordinator still held for the removed patient.
        runtime.coordinator.async_set_configured_patients(runtime.patient_ids)
        _async_bind_runtime(hass, key, runtime)
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
def _async_account_reauth_entry_id(hass: HomeAssistant, key: str) -> str | None:
    """The entry an open reauth dialog of this account belongs to, if any.

    Home Assistant deduplicates reauth flows per config entry, and every entry
    of one LibreLinkUp account runs into the very same rejected password. Which
    account a flow belongs to is therefore asked of its config entry, never of
    runtime.entries: a setup that fails on the password releases its entry
    before the account's next entry retries, so the entry an open flow was
    started for is routinely gone from the runtime by then.
    """
    for flow in hass.config_entries.flow.async_progress_by_handler(
        DOMAIN,
        match_context={"source": SOURCE_REAUTH},
        include_uninitialized=True,
    ):
        entry_id = flow["context"].get("entry_id")

        if entry_id is None:
            continue

        entry = hass.config_entries.async_get_entry(entry_id)

        # ".get" rather than account_key(entry), as in the config flow: an entry
        # that carries no address at all must not be matched against an
        # unrelated account.
        if entry is not None and account_key(entry.data.get(CONF_EMAIL, "")) == key:
            return entry_id

    return None


@callback
def _async_bind_runtime(
    hass: HomeAssistant, key: str, runtime: AccountRuntime
) -> None:
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

        if _async_account_reauth_entry_id(hass, key) is not None:
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
                async_create_issue(hass, entry, ISSUE_SHARE_REVOKED)
            else:
                async_delete_issue(hass, entry_id, ISSUE_SHARE_REVOKED)

    @callback
    def report_account_state(problem: bool) -> None:
        if not problem:
            for entry_id in runtime.entries:
                async_delete_issue(hass, entry_id, ISSUE_ACCOUNT_STATE)
            return

        if not runtime.entries:
            return

        # One issue per account, on the same entry that would be asked to
        # reauth, so a three-patient account does not show three identical cards.
        entry = hass.config_entries.async_get_entry(min(runtime.entries))

        if entry is not None:
            async_create_issue(hass, entry, ISSUE_ACCOUNT_STATE)

    coordinator.async_request_reauth = request_reauth
    coordinator.async_report_missing_patients = report_missing_patients
    coordinator.async_report_account_state = report_account_state
