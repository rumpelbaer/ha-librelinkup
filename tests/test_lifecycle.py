"""Shared account lifecycle: reauth recovery, polling preference, shutdown.

These tests drive the real Home Assistant machinery -- real reauth flows, real
config entry reloads, real scheduled refreshes. Nothing here mocks
async_reload: the bug this file exists for lived exactly in the reload.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from helpers import measurement

from custom_components.librelinkup.api import (
    LibreLinkUpAccountStateError,
    LibreLinkUpAuthenticationError,
)
from custom_components.librelinkup.const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DOMAIN,
)
from custom_components.librelinkup.runtime import account_key

EMAIL = "user@example.com"


def make_entry(hass, key, *, password="secret", **kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"LibreLinkUp - Person {key}",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: password,
            CONF_PATIENT_ID: f"patient-{key}",
            CONF_PATIENT_NAME: f"Person {key}",
        },
        unique_id=f"uid-{key}",
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


def snapshot(*entries) -> dict:
    return {entry.data[CONF_PATIENT_ID]: measurement() for entry in entries}


def account_patch(measurements, *, connections=None, side_effect=None):
    """Patch the shared client, for the coordinator and the config flow alike."""
    if connections is None:
        connections = [{"patientId": patient_id} for patient_id in measurements]

    return (
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
            new=AsyncMock(return_value=measurements, side_effect=side_effect),
        ),
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_get_connections",
            new=AsyncMock(return_value=connections),
        ),
    )


async def setup_entries(hass, *entries):
    """Load the given entries with the API mocked out."""
    for entry in entries:
        if entry.state is ConfigEntryState.NOT_LOADED:
            await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def advance(hass, seconds=90):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def runtime_of(hass, entry):
    return hass.data[DOMAIN][account_key(entry)]


# --- C1: the account must keep polling after a successful reauth --------------


@pytest.mark.parametrize("patient_count", [1, 2, 3])
async def test_polling_resumes_after_a_real_reauth(hass, patient_count) -> None:
    """The regression that made a shared account stop polling for good.

    Home Assistant stops scheduling refreshes once an update raised
    ConfigEntryAuthFailed, and expects the entry reload after reauth to build a
    new coordinator. A coordinator shared by several entries survives that
    reload, so nothing restarted it: the reauth dialog reported success while
    every entity stayed unavailable until Home Assistant was restarted.
    """
    entries = [make_entry(hass, key) for key in range(patient_count)]
    measurements = snapshot(*entries)

    login, poll, flow_login, flow_connections = account_patch(measurements)

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, *entries)

        assert all(entry.state is ConfigEntryState.LOADED for entry in entries)
        assert hass.states.get("sensor.person_0_glucose").state == "6.4"

        # The account password was changed at Abbott.
        poll_mock.side_effect = LibreLinkUpAuthenticationError("rejected")
        await entries[0].runtime_data.async_refresh()
        await hass.async_block_till_done()

        flows = [
            flow
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            if flow["context"]["source"] == SOURCE_REAUTH
        ]

        # Exactly one dialog for the whole account, not one per person.
        assert len(flows) == 1

        # The user enters the new password. No mock on async_reload.
        poll_mock.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], user_input={CONF_PASSWORD: "rotated"}
        )
        await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert all(entry.data[CONF_PASSWORD] == "rotated" for entry in entries)
        assert all(entry.state is ConfigEntryState.LOADED for entry in entries)

        # Every person is served again...
        for index in range(patient_count):
            state = hass.states.get(f"sensor.person_{index}_glucose")
            assert state is not None
            assert state.state == "6.4"

        # ...and the account is really polling again, not just holding the
        # snapshot the reload happened to fetch.
        before = poll_mock.await_count
        await advance(hass)

        assert poll_mock.await_count > before


async def test_password_is_adopted_by_the_shared_client_after_reauth(hass) -> None:
    first = make_entry(hass, 0)
    second = make_entry(hass, 1)
    measurements = snapshot(first, second)

    login, poll, flow_login, flow_connections = account_patch(measurements)

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, first, second)
        runtime = runtime_of(hass, first)

        poll_mock.side_effect = LibreLinkUpAuthenticationError("rejected")
        await runtime.coordinator.async_refresh()
        await hass.async_block_till_done()

        flow = next(
            flow
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            if flow["context"]["source"] == SOURCE_REAUTH
        )
        poll_mock.side_effect = None
        await hass.config_entries.flow.async_configure(
            flow["flow_id"], user_input={CONF_PASSWORD: "rotated"}
        )
        await hass.async_block_till_done()

        runtime = runtime_of(hass, first)

        assert runtime.password == "rotated"
        assert runtime.api._password == "rotated"
        # The token obtained with the old password must be gone.
        assert runtime.api._token is None


# --- L13: reauth while the chosen entry is disabled ---------------------------


async def test_reauth_repairs_the_account_when_its_entry_is_disabled(hass) -> None:
    """A disabled entry must not turn a successful password change into an error."""
    from homeassistant.config_entries import ConfigEntryDisabler

    first = make_entry(hass, 0)
    second = make_entry(hass, 1)
    measurements = snapshot(first, second)

    login, poll, flow_login, flow_connections = account_patch(measurements)

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, first, second)

        await hass.config_entries.async_set_disabled_by(
            first.entry_id, ConfigEntryDisabler.USER
        )
        await hass.async_block_till_done()

        assert first.state is ConfigEntryState.NOT_LOADED
        assert second.state is ConfigEntryState.LOADED

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": first.entry_id},
            data=dict(first.data),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_PASSWORD: "rotated"}
        )
        await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        # Both entries carry the new password, and the loaded sibling was
        # reloaded so the shared client adopted it.
        assert first.data[CONF_PASSWORD] == "rotated"
        assert second.data[CONF_PASSWORD] == "rotated"
        assert second.state is ConfigEntryState.LOADED
        assert runtime_of(hass, second).password == "rotated"


# --- L10: one reauth flow per account, not per entry -------------------------


async def test_a_second_auth_failure_does_not_open_a_second_flow(hass) -> None:
    entries = [make_entry(hass, key) for key in range(2)]
    measurements = snapshot(*entries)

    login, poll, flow_login, flow_connections = account_patch(measurements)

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, *entries)
        coordinator = entries[0].runtime_data

        poll_mock.side_effect = LibreLinkUpAuthenticationError("rejected")

        for _ in range(3):
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        flows = [
            flow
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            if flow["context"]["source"] == SOURCE_REAUTH
        ]

        assert len(flows) == 1


async def test_no_second_flow_after_the_chosen_entry_is_removed(hass) -> None:
    """Home Assistant deduplicates per entry; the account has to do the rest."""
    entries = [make_entry(hass, key) for key in range(2)]
    measurements = snapshot(*entries)

    login, poll, flow_login, flow_connections = account_patch(measurements)

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, *entries)
        coordinator = entries[0].runtime_data

        poll_mock.side_effect = LibreLinkUpAuthenticationError("rejected")
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        chosen = min(runtime_of(hass, entries[0]).entries)
        other = next(
            entry for entry in entries if entry.entry_id != chosen
        )

        # Removing the entry aborts its flow; the remaining entry may then open
        # one of its own, but never a second one at the same time.
        await hass.config_entries.async_remove(chosen)
        await hass.async_block_till_done()

        await other.runtime_data.async_refresh()
        await hass.async_block_till_done()
        await other.runtime_data.async_refresh()
        await hass.async_block_till_done()

        flows = [
            flow
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            if flow["context"]["source"] == SOURCE_REAUTH
        ]

        assert len(flows) == 1


# --- M1: the "Enable polling for updates" system option ----------------------


async def test_polling_disabled_for_the_only_entry_stops_polling(hass) -> None:
    entry = make_entry(hass, 0, pref_disable_polling=True)
    login, poll, flow_login, flow_connections = account_patch(snapshot(entry))

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, entry)

        assert entry.state is ConfigEntryState.LOADED
        # The initial poll still happens: setup needs data.
        assert poll_mock.await_count == 1
        assert entry.runtime_data.update_interval is None

        await advance(hass, seconds=600)

        assert poll_mock.await_count == 1


async def test_one_entry_that_allows_polling_keeps_the_account_polling(hass) -> None:
    first = make_entry(hass, 0, pref_disable_polling=True)
    second = make_entry(hass, 1)
    login, poll, flow_login, flow_connections = account_patch(snapshot(first, second))

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, first, second)

        assert first.runtime_data.update_interval is not None

        before = poll_mock.await_count
        await advance(hass)

        assert poll_mock.await_count > before


async def test_polling_stops_once_every_entry_opts_out(hass) -> None:
    first = make_entry(hass, 0, pref_disable_polling=True)
    second = make_entry(hass, 1)
    login, poll, flow_login, flow_connections = account_patch(snapshot(first, second))

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, first, second)
        coordinator = first.runtime_data

        # Turning the option on reloads the entry, which is what makes the
        # account recompute its preference.
        hass.config_entries.async_update_entry(second, pref_disable_polling=True)
        await hass.config_entries.async_reload(second.entry_id)
        await hass.async_block_till_done()

        assert second.runtime_data.update_interval is None

        stopped = poll_mock.await_count
        await advance(hass, seconds=600)

        assert poll_mock.await_count == stopped

        # A manual refresh still works while automatic polling is off.
        await second.runtime_data.async_refresh()

        assert poll_mock.await_count == stopped + 1


async def test_polling_resumes_when_the_option_is_turned_back_on(hass) -> None:
    entry = make_entry(hass, 0, pref_disable_polling=True)
    login, poll, flow_login, flow_connections = account_patch(snapshot(entry))

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, entry)

        hass.config_entries.async_update_entry(entry, pref_disable_polling=False)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.runtime_data.update_interval is not None

        before = poll_mock.await_count
        await advance(hass)

        assert poll_mock.await_count > before


async def test_a_backoff_never_switches_polling_back_on(hass) -> None:
    from aiohttp import ClientResponseError, RequestInfo
    from multidict import CIMultiDict, CIMultiDictProxy

    entry = make_entry(hass, 0, pref_disable_polling=True)
    login, poll, flow_login, flow_connections = account_patch(snapshot(entry))

    url = "https://api-de.libreview.io/llu/connections"
    error = ClientResponseError(
        RequestInfo(url=url, method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=url),
        (),
        status=429,
        message="Too Many Requests",
        headers=CIMultiDictProxy(CIMultiDict({"Retry-After": "120"})),
    )

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, entry)
        coordinator = entry.runtime_data

        poll_mock.side_effect = error
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.update_interval is None


# --- M2: the shared coordinator has to stop on shutdown ----------------------


async def test_coordinator_is_registered_for_shutdown(hass) -> None:
    entry = make_entry(hass, 0)
    login, poll, flow_login, flow_connections = account_patch(snapshot(entry))

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, entry)
        coordinator = entry.runtime_data

        # Home Assistant only offers this for coordinators without a config
        # entry, and it is the only thing that cancels the refresh on shutdown.
        assert coordinator.config_entry is None
        assert coordinator._unsub_shutdown is not None

        hass.bus.async_fire("homeassistant_stop")
        await hass.async_block_till_done()

        stopped = poll_mock.await_count
        await advance(hass, seconds=600)

        assert poll_mock.await_count == stopped


# --- L2: two entries of one account being set up at the same time ------------


async def test_parallel_setup_shares_one_runtime(hass) -> None:
    first = make_entry(hass, 0)
    second = make_entry(hass, 1)
    measurements = snapshot(first, second)

    login, poll, flow_login, flow_connections = account_patch(measurements)

    with login, poll as poll_mock, flow_login, flow_connections:
        await asyncio.gather(
            hass.config_entries.async_setup(first.entry_id),
            hass.config_entries.async_setup(second.entry_id),
            return_exceptions=True,
        )
        await hass.async_block_till_done()

        assert first.state is ConfigEntryState.LOADED
        assert second.state is ConfigEntryState.LOADED

        runtime = runtime_of(hass, first)

        assert len(hass.data[DOMAIN]) == 1
        assert set(runtime.entries) == {first.entry_id, second.entry_id}
        assert first.runtime_data is second.runtime_data
        assert runtime.coordinator.api is runtime.api
        # An entry only polls when the snapshot holds nothing for its own
        # patient, so at worst one request per entry -- never one per patient
        # per interval.
        assert poll_mock.await_count <= 2

        for index in (0, 1):
            assert hass.states.get(f"sensor.person_{index}_glucose").state == "6.4"


async def test_two_entries_never_poll_the_account_at_the_same_time(hass) -> None:
    """The setup lock, with a poll that is deliberately still in flight.

    The first entry is loaded normally so the component is up; the next two are
    then set up concurrently, which is what Home Assistant does with the entries
    of one integration.
    """
    first = make_entry(hass, 0)
    measurements = {
        f"patient-{key}": measurement() for key in (0, 1, 2)
    }

    release = asyncio.Event()
    concurrent: list[int] = []
    active = 0

    async def blocking_poll():
        nonlocal active
        active += 1
        concurrent.append(active)
        await release.wait()
        active -= 1
        return measurements

    login, poll, flow_login, flow_connections = account_patch(measurements)

    with login, poll, flow_login, flow_connections:
        await setup_entries(hass, first)

    # Only now, so loading the component does not set them up along the way.
    second = make_entry(hass, 1)
    third = make_entry(hass, 2)

    with (
        login,
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
            new=AsyncMock(side_effect=blocking_poll),
        ) as poll_mock,
        flow_login,
        flow_connections,
    ):
        setups = asyncio.gather(
            hass.config_entries.async_setup(second.entry_id),
            hass.config_entries.async_setup(third.entry_id),
            return_exceptions=True,
        )
        # Let both setups get as far as they can.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await setups
        await hass.async_block_till_done()

        assert second.state is ConfigEntryState.LOADED
        assert third.state is ConfigEntryState.LOADED
        # Never two polls of one account in flight at once...
        assert concurrent and max(concurrent) == 1
        # ...and the one poll covered both people, because both had registered
        # before it started.
        assert poll_mock.await_count == 1
        assert hass.states.get("sensor.person_1_glucose").state == "6.4"
        assert hass.states.get("sensor.person_2_glucose").state == "6.4"


# --- I5: a person added to a running account --------------------------------


async def test_a_new_person_does_not_wait_for_the_next_interval(hass) -> None:
    first = make_entry(hass, 0)
    login, poll, flow_login, flow_connections = account_patch(snapshot(first))

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, first)

        assert poll_mock.await_count == 1

        second = make_entry(hass, 1)
        poll_mock.return_value = snapshot(first, second)

        await setup_entries(hass, second)

        # Available right away, without waiting out the remaining interval.
        assert hass.states.get("sensor.person_1_glucose").state == "6.4"
        # And it cost exactly one extra request.
        assert poll_mock.await_count == 2


async def test_unloading_an_entry_drops_its_patient_from_the_snapshot(hass) -> None:
    """Privacy before efficiency: a reload refetches instead of reusing old data."""
    first = make_entry(hass, 0)
    second = make_entry(hass, 1)
    login, poll, flow_login, flow_connections = account_patch(snapshot(first, second))

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, first, second)
        coordinator = first.runtime_data

        assert "patient-1" in coordinator.data

        await hass.config_entries.async_unload(second.entry_id)
        await hass.async_block_till_done()

        # No glucose value of an unloaded entry is left behind in memory.
        assert "patient-1" not in coordinator.data
        assert "patient-1" not in coordinator._measured_at
        assert "patient-0" in coordinator.data

        polls = poll_mock.await_count
        await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()

        assert poll_mock.await_count == polls + 1
        assert hass.states.get("sensor.person_1_glucose").state == "6.4"


# --- H3: an account state problem must not ask for the password --------------


async def test_account_state_error_raises_no_reauth_flow(hass) -> None:
    entry = make_entry(hass, 0)
    login, poll, flow_login, flow_connections = account_patch(snapshot(entry))

    with login, poll as poll_mock, flow_login, flow_connections:
        await setup_entries(hass, entry)

        poll_mock.side_effect = LibreLinkUpAccountStateError("terms of use")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

        flows = [
            flow
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            if flow["context"]["source"] == SOURCE_REAUTH
        ]

        assert not flows


# --- L5: an unsupported region is permanent, not a network hiccup ------------


async def test_an_unsupported_region_fails_permanently(hass) -> None:
    """Retrying every minute cannot make an unknown region appear."""
    from homeassistant.config_entries import ConfigEntryState

    from custom_components.librelinkup.api import LibreLinkUpRegionError

    entry = make_entry(hass, 0)
    login, poll, flow_login, flow_connections = account_patch(snapshot(entry))

    with (
        login,
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
            new=AsyncMock(side_effect=LibreLinkUpRegionError("Unsupported region: xx")),
        ) as poll_mock,
        flow_login,
        flow_connections,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # SETUP_ERROR, not SETUP_RETRY: Home Assistant stops trying.
        assert entry.state is ConfigEntryState.SETUP_ERROR

        polls = poll_mock.await_count
        await advance(hass, seconds=600)

        assert poll_mock.await_count == polls
        # And the account runtime was released again.
        assert not hass.data.get(DOMAIN)


async def test_rejected_credentials_fail_setup_and_release_the_account(hass) -> None:
    """The third setup outcome: a password the account no longer accepts.

    Unlike a region error this asks the user for their password again rather
    than failing permanently -- and, like every failed setup, it has to leave
    the shared runtime exactly as it found it.
    """
    from homeassistant.config_entries import ConfigEntryState

    from custom_components.librelinkup.api import LibreLinkUpAuthenticationError

    entry = make_entry(hass, 0)
    login, poll, flow_login, flow_connections = account_patch(snapshot(entry))

    with (
        login,
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
            new=AsyncMock(side_effect=LibreLinkUpAuthenticationError("rejected")),
        ),
        flow_login,
        flow_connections,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # ConfigEntryAuthFailed, so Home Assistant asks for the password again.
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert [
            flow
            for flow in hass.config_entries.flow.async_progress()
            if flow["context"]["source"] == SOURCE_REAUTH
        ]

        # Nothing of the entry stayed behind: no account runtime, and the entry
        # does not point at a coordinator it was released from.
        assert not hass.data.get(DOMAIN)
        assert getattr(entry, "runtime_data", None) is None


async def test_a_network_failure_still_gets_retried(hass) -> None:
    """The counterpart: a transient failure must stay a retry."""
    from aiohttp import ClientConnectionError
    from homeassistant.config_entries import ConfigEntryState

    entry = make_entry(hass, 0)
    login, poll, flow_login, flow_connections = account_patch(snapshot(entry))

    with (
        login,
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
            new=AsyncMock(side_effect=ClientConnectionError("down")),
        ),
        flow_login,
        flow_connections,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.SETUP_RETRY
